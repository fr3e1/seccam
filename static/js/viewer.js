// Dashboard: live camera grid (proto 2 binary frames), events, notifications, settings drawer.
import { api, wsUrl } from './api.js';
import { $, h, toast, fmtTime, fmtRelative, fmtBytes } from './ui.js';
import { syncThemeFromSettings } from './theme.js';
import { backoffDelay } from './backoff.js';
import { decodeViewerFrame } from './frame.js';
import { ALL_LABELS, labelCategory } from './camera-logic.js';
import { openSettings, closeSettings, isSettingsOpen, setCurrentSettings, signOut } from './settings.js';

const PAGE = 50;
const MAX_EVENTS = 500;
const PREFS_KEY = 'seccam_prefs';

const tiles = new Map();
let settings = null;
let events = [];
let filterLabels = new Set();
let filterCamera = '';
let lastPageCount = PAGE;
let loading = false;
let ws = null;
let attempt = 0;
let reconnectTimer = null;
let stableTimer = null;
let lastMsgAt = 0;
let prefs = { notifications: false, beep: false };
let unread = 0;
let audioCtx = null;
const lastNotif = new Map();
let openEvent = null;

const grid = $('#camera-grid');
const eventsEl = $('#events');

// ---------- label colours for canvas drawing (read from the theme variables) ----------
let colorCache = { theme: null, map: {} };
function labelColor(label) {
  const theme = document.documentElement.dataset.theme;
  if (colorCache.theme !== theme) colorCache = { theme, map: {} };
  const cat = labelCategory(label);
  if (!colorCache.map[cat]) colorCache.map[cat] = getComputedStyle(document.documentElement).getPropertyValue(`--label-${cat}`).trim() || '#ffffff';
  return colorCache.map[cat];
}

// ---------- camera tiles ----------
function modeBadge(mode) {
  return mode === 'local' ? 'badge badge-ok' : mode === 'server' ? 'badge badge-warn' : 'badge';
}

function createTile(cam) {
  const name = h('span', { class: 'tile-name' });
  const mode = h('span', {});
  const fps = h('span', { class: 'muted' });
  const canvas = h('canvas', { 'aria-label': 'Live view' });
  const empty = h('div', { class: 'tile-empty' }, 'waiting for frames');
  const el = h('div', { class: 'tile', dataset: { cameraId: cam.camera_id } },
    h('div', { class: 'tile-head' }, name, mode, fps), h('div', { class: 'tile-view' }, canvas, empty));
  grid.append(el);
  return { el, name, mode, fps, canvas, empty, bitmap: null, header: null, next: null, decoding: false, raf: 0, removed: false, hasFrame: false };
}

function updateTile(t, cam) {
  t.name.textContent = cam.name || cam.camera_id;
  t.mode.className = modeBadge(cam.mode);
  t.mode.textContent = cam.mode;
  t.fps.textContent = `${Number(cam.fps || 0).toFixed(1)} fps${cam.infer_ms ? ` ${Math.round(cam.infer_ms)} ms` : ''}`;
}

function onCameras(list) {
  const seen = new Set();
  for (const cam of list) {
    seen.add(cam.camera_id);
    let t = tiles.get(cam.camera_id);
    if (!t) { t = createTile(cam); tiles.set(cam.camera_id, t); }
    updateTile(t, cam);
  }
  for (const [id, t] of tiles) {
    if (seen.has(id)) continue;
    t.removed = true;
    if (t.raf) cancelAnimationFrame(t.raf);
    if (t.bitmap && t.bitmap.close) t.bitmap.close();
    t.el.remove();
    tiles.delete(id);
  }
  $('#grid-empty').classList.toggle('hidden', tiles.size > 0);
  renderFilters();
}

async function toBitmap(jpeg) {
  const blob = new Blob([jpeg], { type: 'image/jpeg' });
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  // very old Safari: decode through an <img>
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally { URL.revokeObjectURL(url); }
}

function onFrame(buf) {
  let f;
  try { f = decodeViewerFrame(buf); } catch { return; }
  const t = tiles.get(f.header.camera_id);
  if (!t) return;
  t.next = f; // the newest frame replaces any not yet decoded one
  if (!t.decoding) pump(t);
}

async function pump(t) {
  t.decoding = true;
  try {
    while (t.next && !t.removed) {
      const f = t.next;
      t.next = null;
      try {
        const bmp = await toBitmap(f.jpeg);
        if (t.removed) { if (bmp.close) bmp.close(); break; }
        if (t.bitmap && t.bitmap.close) t.bitmap.close();
        t.bitmap = bmp;
        t.header = f.header;
        schedule(t);
      } catch { /* corrupt jpeg: skip */ }
    }
  } finally { t.decoding = false; }
}

function schedule(t) {
  if (t.raf) return;
  t.raf = requestAnimationFrame(() => { t.raf = 0; draw(t); });
}

function draw(t) {
  const bmp = t.bitmap;
  if (!bmp || t.removed) return;
  const c = t.canvas;
  const w = bmp.width;
  const hgt = bmp.height;
  if (c.width !== w || c.height !== hgt) { c.width = w; c.height = hgt; }
  const ctx = c.getContext('2d');
  ctx.drawImage(bmp, 0, 0);
  if (!t.hasFrame) { t.hasFrame = true; t.empty.classList.add('hidden'); }
  const dets = (t.header && t.header.detections) || [];
  const fw = (t.header && t.header.frame_w) || w;
  const fh = (t.header && t.header.frame_h) || hgt;
  const sx = w / fw;
  const sy = hgt / fh;
  const minScore = settings ? settings.min_score : 0;
  const enabled = settings && Array.isArray(settings.labels) ? new Set(settings.labels) : null;
  const lw = Math.max(2, Math.round(w / 320));
  ctx.lineWidth = lw;
  ctx.font = `${Math.max(12, Math.round(w / 40))}px monospace`;
  ctx.textBaseline = 'top';
  for (const d of dets) {
    if (d.score < minScore || (enabled && !enabled.has(d.label))) continue;
    const [x, y, bw, bh] = d.bbox;
    const color = labelColor(d.label);
    ctx.strokeStyle = color;
    ctx.strokeRect(x * sx, y * sy, bw * sx, bh * sy);
    const text = `${d.label} ${Math.round(d.score * 100)}%`;
    const th = Math.max(12, Math.round(w / 40)) + 4;
    const ty = y * sy - th < 0 ? y * sy : y * sy - th;
    ctx.fillStyle = color;
    ctx.fillRect(x * sx, ty, ctx.measureText(text).width + 8, th);
    ctx.fillStyle = '#000';
    ctx.fillText(text, x * sx + 4, ty + 2);
  }
}

// ---------- events ----------
function chip(label) { return h('span', { class: `lbl lbl-${labelCategory(label)}` }, label); }

function matches(ev) {
  if (filterCamera && ev.camera_id !== filterCamera) return false;
  return filterLabels.size === 0 || ev.labels.some((l) => filterLabels.has(l));
}

function renderFilters() {
  const box = $('#filters');
  const nodes = [];
  const all = h('button', { class: 'filter', type: 'button', 'aria-pressed': filterLabels.size === 0 ? 'true' : 'false' }, 'all');
  all.addEventListener('click', () => { filterLabels.clear(); renderFilters(); renderEvents(); });
  nodes.push(all);
  for (const l of ALL_LABELS) {
    const b = h('button', { class: 'filter', type: 'button', 'aria-pressed': filterLabels.has(l) ? 'true' : 'false' }, l);
    b.addEventListener('click', () => { filterLabels.has(l) ? filterLabels.delete(l) : filterLabels.add(l); renderFilters(); renderEvents(); });
    nodes.push(b);
  }
  if (tiles.size > 1) {
    const sel = h('select', { class: 'select', 'aria-label': 'Filter by camera' }, h('option', { value: '' }, 'all cameras'));
    for (const [id, t] of tiles) sel.append(h('option', { value: id, selected: id === filterCamera }, t.name.textContent || id));
    sel.addEventListener('change', () => { filterCamera = sel.value; renderEvents(); });
    nodes.push(sel);
  }
  box.replaceChildren(...nodes);
}

function eventItem(ev, fresh = false) {
  const thumb = h('img', { class: 'event-thumb', alt: '', loading: 'lazy', src: ev.thumb_url || null });
  const item = h('button', { class: fresh ? 'event-item event-new' : 'event-item', type: 'button', dataset: { ts: ev.ts } },
    thumb,
    h('div', { class: 'event-info' },
      h('div', {}, ev.labels.map((l) => chip(l))),
      h('div', {}, ev.camera_name || ev.camera_id),
      h('div', { class: 'muted ev-time' }, fmtRelative(ev.ts)),
    ),
    h('span', { class: 'muted' }, ev.max_score != null ? `${Math.round(ev.max_score * 100)}%` : ''));
  item.addEventListener('click', () => showEvent(ev));
  return item;
}

function renderEvents() {
  const shown = events.filter(matches);
  eventsEl.replaceChildren(...(shown.length ? shown.map((e) => eventItem(e)) : [h('p', { class: 'muted' }, 'no events')]));
  const more = $('#more-btn');
  more.disabled = loading || lastPageCount < PAGE;
  more.textContent = loading ? 'loading' : 'load more';
}

async function loadEvents(before = null) {
  if (loading) return;
  loading = true;
  renderEvents();
  try {
    const q = new URLSearchParams({ limit: String(PAGE) });
    if (before !== null) q.set('before', String(before));
    const r = await api(`/api/events?${q}`);
    lastPageCount = r.events.length;
    events = before === null ? r.events : events.concat(r.events);
    if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
  } catch (e) {
    if (e.status !== 401) toast('Could not load events', 'err');
  } finally { loading = false; renderEvents(); }
}

function onEvent(ev) {
  events.unshift(ev);
  if (events.length > MAX_EVENTS) events.pop();
  if (matches(ev)) {
    const first = eventsEl.firstElementChild;
    if (first && !first.classList.contains('event-item')) first.remove();
    eventsEl.prepend(eventItem(ev, true));
    while (eventsEl.children.length > MAX_EVENTS) eventsEl.lastElementChild.remove();
  }
  loadStats();
  if (document.hidden) notify(ev);
}

function showEvent(ev) {
  openEvent = ev;
  $('#event-image').src = `/api/events/${encodeURIComponent(ev.id)}/snapshot.jpg`;
  const rows = [
    ['camera', ev.camera_name || ev.camera_id], ['time', fmtTime(ev.ts)], ['labels', ev.labels.join(', ')],
    ['max score', ev.max_score != null ? `${(ev.max_score * 100).toFixed(1)}%` : '-'],
    ['frame', `${ev.frame_w}x${ev.frame_h}`], ['detections', String(ev.detections.length)], ['id', String(ev.id)],
  ];
  $('#event-details').replaceChildren(...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
  const m = $('#event-modal');
  m.classList.add('modal-open');
  m.setAttribute('aria-hidden', 'false');
  $('#event-close').focus();
}

function closeEvent() {
  openEvent = null;
  const m = $('#event-modal');
  m.classList.remove('modal-open');
  m.setAttribute('aria-hidden', 'true');
  $('#event-image').removeAttribute('src');
}

async function deleteEvent() {
  if (!openEvent || !confirm('Delete this event?')) return;
  const id = openEvent.id;
  try {
    await api(`/api/events/${encodeURIComponent(id)}`, { method: 'DELETE' });
    events = events.filter((e) => e.id !== id);
    closeEvent();
    renderEvents();
    loadStats();
    toast('Event deleted', 'ok');
  } catch (e) { toast(e.detail || 'Could not delete the event', 'err'); }
}

async function loadStats() {
  try {
    const s = await api('/api/stats');
    $('#stats').replaceChildren(h('span', {}, `total ${s.events_total}`), h('span', {}, `24h ${s.events_24h}`), h('span', {}, `disk ${fmtBytes(s.disk_bytes)}`));
  } catch { /* stats are optional */ }
}

// ---------- notifications ----------
function loadPrefs() {
  try { prefs = { ...prefs, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') }; } catch { /* defaults */ }
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') prefs.notifications = false;
  paintPrefs();
}
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private mode */ } }
function paintPrefs() {
  const n = $('#notif-btn');
  n.setAttribute('aria-pressed', String(prefs.notifications));
  n.textContent = `notify: ${prefs.notifications ? 'on' : 'off'}`;
  const b = $('#beep-btn');
  b.setAttribute('aria-pressed', String(prefs.beep));
  b.textContent = `beep: ${prefs.beep ? 'on' : 'off'}`;
}

async function toggleNotifications() {
  if (prefs.notifications) { prefs.notifications = false; } else {
    if (typeof Notification === 'undefined') { toast('Notifications are not supported in this browser', 'warn'); return; }
    let perm = Notification.permission;
    if (perm === 'default') { try { perm = await Notification.requestPermission(); } catch { perm = 'denied'; } }
    prefs.notifications = perm === 'granted';
    if (!prefs.notifications) toast('Notifications are blocked in the browser settings', 'warn');
  }
  savePrefs();
  paintPrefs();
}

function toggleBeep() {
  prefs.beep = !prefs.beep;
  if (prefs.beep) beep(); // also unlocks audio on iOS (needs a user gesture)
  savePrefs();
  paintPrefs();
}

async function beep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    audioCtx = audioCtx || new AC();
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    gain.gain.value = 0.1;
    osc.frequency.value = 880;
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.12);
  } catch { /* audio is best effort */ }
}

function notify(ev) {
  const now = Date.now();
  if (now - (lastNotif.get(ev.camera_id) || 0) < 10000) return;
  lastNotif.set(ev.camera_id, now);
  unread++;
  document.title = `(${unread}) SecCam`;
  if (prefs.beep) beep();
  if (!prefs.notifications || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  try {
    new Notification(`SecCam: ${ev.labels[0] || 'motion'}`, {
      body: `${ev.camera_name || ev.camera_id} ${ev.max_score != null ? Math.round(ev.max_score * 100) + '%' : ''}`.trim(),
      tag: `seccam-${ev.camera_id}`,
    });
  } catch { /* Android Chrome only allows notifications from a service worker */ }
}

// ---------- websocket ----------
function setConn(on) {
  const c = $('#conn');
  c.className = on ? 'badge badge-ok' : 'badge badge-err';
  c.textContent = on ? 'live' : 'offline';
}

// A password change rotates this session's cookie and closes sockets with 4401: probe before leaving.
async function handleUnauthorized() {
  try { await api('/api/me', { redirectOn401: false }); } catch {
    location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search));
    return;
  }
  attempt = 0;
  connect();
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(reconnectTimer);
  const sock = new WebSocket(wsUrl('/ws/viewer?proto=2'));
  ws = sock;
  sock.binaryType = 'arraybuffer';
  sock.onopen = () => {
    lastMsgAt = Date.now();
    setConn(true);
    clearTimeout(stableTimer);
    stableTimer = setTimeout(() => { attempt = 0; }, 30000); // backoff resets only after a stable connection
  };
  sock.onmessage = (e) => {
    lastMsgAt = Date.now();
    if (typeof e.data !== 'string') { onFrame(e.data); return; }
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === 'cameras') onCameras(m.cameras);
    else if (m.type === 'settings') { settings = m.settings; syncThemeFromSettings(settings); setCurrentSettings(settings); }
    else if (m.type === 'event') onEvent(m.event);
  };
  sock.onclose = (e) => {
    if (ws !== sock) return;
    setConn(false);
    clearTimeout(stableTimer);
    if (e.code === 4401) { handleUnauthorized(); return; }
    if (e.code === 4403) { location.assign('/welcome'); return; }
    reconnectTimer = setTimeout(connect, backoffDelay(attempt++));
  };
  sock.onerror = () => { /* onclose follows */ };
}

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 20000);
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastMsgAt > 45000) ws.close(); // 45 s of silence = dead
  for (const el of eventsEl.querySelectorAll('.event-item[data-ts]')) {
    el.querySelector('.ev-time').textContent = fmtRelative(Number(el.dataset.ts));
  }
}, 5000);

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  unread = 0;
  document.title = 'SecCam';
  if (!ws || ws.readyState > WebSocket.OPEN) { attempt = 0; connect(); }
  else if (ws.readyState === WebSocket.OPEN && Date.now() - lastMsgAt > 45000) ws.close(); // dead after sleep
});

// ---------- wiring ----------
function closeAll() {
  if (isSettingsOpen()) closeSettings();
  if (openEvent) closeEvent();
}

$('#settings-btn').addEventListener('click', () => openSettings('detection'));
$('#cam-btn').addEventListener('click', () => openSettings('devices'));
$('#settings-close').addEventListener('click', closeSettings);
$('#scrim').addEventListener('click', closeSettings);
$('#logout-btn').addEventListener('click', signOut);
$('#notif-btn').addEventListener('click', toggleNotifications);
$('#beep-btn').addEventListener('click', toggleBeep);
$('#more-btn').addEventListener('click', () => { if (events.length) loadEvents(events[events.length - 1].id); });
$('#event-close').addEventListener('click', closeEvent);
$('#event-delete').addEventListener('click', deleteEvent);
$('#event-modal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeEvent(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAll(); });

async function init() {
  loadPrefs();
  renderFilters();
  setConn(false);
  connect();
  try {
    settings = await api('/api/settings');
    syncThemeFromSettings(settings);
    setCurrentSettings(settings);
  } catch { /* the WS settings message fills it in */ }
  loadEvents();
  loadStats();
  setInterval(loadStats, 60000);
}

init();
