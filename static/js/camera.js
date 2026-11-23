// Camera page: getUserMedia capture, local (TensorFlow.js) or server detection, frame upload over /ws/camera.
// Under a device principal no /api/* call is authorised, so this page never calls api() or saveTheme.
import { api, wsUrl } from './api.js';
import { $, h, toast } from './ui.js';
import { syncThemeFromSettings } from './theme.js';
import { backoffDelay } from './backoff.js';
import {
  shouldSkipFrame, captureConstraints, pickDevice, sendIntervalMs, closeReason, detectionsChanged, labelCategory, canFallbackToServer, inferenceTooSlow,
} from './camera-logic.js';

const DEVICE_KEY = 'seccam.cameraDevice';
const ID_KEY = 'seccam_camera_id';
const NAME_KEY = 'seccam_camera_name';

let settings = {
  labels: ['person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'cat', 'dog', 'bird'],
  min_score: 0.55, cooldown_s: 30, preview_fps: 2, detect_fps: 2, mode: 'auto', retention_days: 14,
  jpeg_quality: 0.7, capture_width: 960,
};
let cameraId = null;
let cameraName = '';
let ws = null;
let stream = null;
let running = false;
let visible = true;
let effectiveMode = 'starting';
let modeChosen = false;
let model = null;
let attempt = 0;
let reconnectTimer = null;
let stableTimer = null;
let pingTimer = null;
let wakeLock = null;
let dimmed = false;
let mirrored = false;
let currentDeviceId = null;
let cams = [];
let loopGen = 0;
let detectTimer = null;
let previewTimer = null;
let encoding = false;
let encodeSince = 0;
let capCanvas = null;
let sent = [];
let lastInferMs = 0;
let autoDecided = false;
let autoFellBack = false;
let allSamples = [];
let pending = null;
let pendingAt = 0;
let lastSentDets = null;
let lastSentDetsAt = 0;
let lastFrameAt = 0;
let configWaiter = null;

const el = {
  status: $('#status'), mode: $('#mode'), fps: $('#fps'), infer: $('#infer'), labels: $('#hud-labels'),
  video: $('#video'), overlay: $('#overlay'), dimLayer: $('#dim-layer'), viewfinder: $('#viewfinder'),
  setup: $('#setup-card'), name: $('#cam-name'), pickerField: $('#picker-field'), picker: $('#picker'),
  start: $('#start-btn'), wakeHint: $('#wake-hint'), insecure: $('#insecure-card'), revoked: $('#revoked-card'),
  replaced: $('#replaced-card'), error: $('#error'), controls: $('#controls'), dimBtn: $('#dim-btn'),
  switchBtn: $('#switch-btn'), stopBtn: $('#stop-btn'),
};

const show = (e) => e.classList.remove('hidden');
const hide = (e) => e.classList.add('hidden');

// ---------- storage (blocked in some private modes) ----------
function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }
function lsDel(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } }

function loadIdentity() {
  // Admin-session cameras use a stable local id; for paired devices the server's config.camera_id replaces it.
  cameraId = lsGet(ID_KEY);
  if (!cameraId || !/^[A-Za-z0-9_-]{1,64}$/.test(cameraId)) {
    const rnd = crypto.getRandomValues(new Uint8Array(4));
    cameraId = 'cam-' + Array.from(rnd, (b) => b.toString(16).padStart(2, '0')).join('');
    lsSet(ID_KEY, cameraId);
  }
  cameraName = lsGet(NAME_KEY) || '';
  el.name.value = cameraName;
}

// ---------- HUD ----------
function setStatus(text, kind = '') {
  el.status.textContent = text;
  el.status.className = kind ? `badge badge-${kind}` : 'badge';
}
function setModeBadge(mode) {
  el.mode.textContent = { local: 'on-device AI', server: 'server AI', starting: 'starting' }[mode] || mode;
  el.mode.className = mode === 'local' ? 'badge badge-ok' : mode === 'server' ? 'badge badge-warn' : 'badge';
}
function showError(msg) { el.error.textContent = msg; show(el.error); }

function visibleDets() {
  return (pending || []).filter((d) => d.score >= settings.min_score && settings.labels.includes(d.label));
}

function updateHud() {
  const now = Date.now();
  sent = sent.filter((t) => now - t <= 2000);
  el.fps.textContent = `${(sent.length / 2).toFixed(1)} fps`;
  el.infer.textContent = lastInferMs > 0 ? `${lastInferMs} ms` : '- ms';
  if (pending && now - pendingAt > 3000) { pending = null; clearOverlay(); }
  const seen = new Set();
  el.labels.replaceChildren(...visibleDets().filter((d) => !seen.has(d.label) && seen.add(d.label))
    .map((d) => h('span', { class: `lbl lbl-${labelCategory(d.label)}` }, d.label)));
}
setInterval(updateHud, 1000);

// ---------- overlay ----------
let ctxO = null;
let layout = { scale: 1, ox: 0, oy: 0, w: 0, h: 0 };
const colors = { theme: null, map: {} };
function color(label) {
  const theme = document.documentElement.dataset.theme;
  if (colors.theme !== theme) { colors.theme = theme; colors.map = {}; }
  const cat = labelCategory(label);
  if (!colors.map[cat]) colors.map[cat] = getComputedStyle(document.documentElement).getPropertyValue(`--label-${cat}`).trim() || '#ffffff';
  return colors.map[cat];
}

function resizeOverlay() {
  const r = el.viewfinder.getBoundingClientRect();
  const vw = el.video.videoWidth;
  const vh = el.video.videoHeight;
  const dpr = window.devicePixelRatio || 1;
  el.overlay.width = Math.max(1, Math.round(r.width * dpr));
  el.overlay.height = Math.max(1, Math.round(r.height * dpr));
  ctxO = el.overlay.getContext('2d');
  ctxO.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!vw || !vh) return;
  const scale = Math.min(r.width / vw, r.height / vh);
  layout = { scale, ox: (r.width - vw * scale) / 2, oy: (r.height - vh * scale) / 2, w: r.width, h: r.height };
  if (pending) drawBoxes(pending, el.video.videoWidth, el.video.videoHeight);
}

function clearOverlay() {
  if (ctxO) { ctxO.save(); ctxO.setTransform(1, 0, 0, 1, 0, 0); ctxO.clearRect(0, 0, el.overlay.width, el.overlay.height); ctxO.restore(); }
}

// Detections are in frame_w x frame_h space; map to the video, then to the letterboxed display area.
function drawBoxes(dets, srcW, srcH) {
  clearOverlay();
  const vw = el.video.videoWidth;
  const vh = el.video.videoHeight;
  if (!ctxO || !vw || !vh || !srcW || !srcH) return;
  ctxO.lineWidth = 2;
  ctxO.font = '12px monospace';
  ctxO.textBaseline = 'top';
  for (const d of dets) {
    if (d.score < settings.min_score || !settings.labels.includes(d.label)) continue;
    const [x, y, w, hh] = d.bbox;
    const k = layout.scale;
    const bw = (w * vw / srcW) * k;
    const bh = (hh * vh / srcH) * k;
    let bx = layout.ox + (x * vw / srcW) * k;
    const by = layout.oy + (y * vh / srcH) * k;
    if (mirrored) bx = layout.w - bx - bw;
    const c = color(d.label);
    ctxO.strokeStyle = c;
    ctxO.strokeRect(bx, by, bw, bh);
    const text = `${d.label} ${Math.round(d.score * 100)}%`;
    const tw = ctxO.measureText(text).width + 8;
    const ty = by - 16 < 0 ? by : by - 16;
    ctxO.fillStyle = c;
    ctxO.fillRect(bx, ty, tw, 16);
    ctxO.fillStyle = '#000';
    ctxO.fillText(text, bx + 4, ty + 2);
  }
}

// ---------- wake lock ----------
async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) { el.wakeHint.textContent = 'Screen wake lock is not supported here: set the screen timeout to never.'; return; }
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
    el.wakeHint.textContent = '';
  } catch {
    el.wakeHint.textContent = 'Could not keep the screen awake: set the screen timeout to never.';
  }
}
function releaseWakeLock() {
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}

// ---------- capture ----------
async function listCams() {
  try {
    return (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
  } catch { return []; }
}

async function refreshPicker() {
  cams = await listCams();
  const many = cams.length > 1;
  el.pickerField.classList.toggle('hidden', !many);
  el.switchBtn.classList.toggle('hidden', !many);
  if (!many) return;
  el.picker.replaceChildren(...cams.map((d, i) => h('option', { value: d.deviceId, selected: d.deviceId === currentDeviceId }, d.label || `camera ${i + 1}`)));
}

function stopStream() {
  if (stream) {
    for (const t of stream.getTracks()) { t.onended = null; t.stop(); }
    stream = null;
  }
  el.video.srcObject = null;
}

async function openStream(preferred) {
  stopStream();
  let id = preferred;
  if (id === undefined) {
    const known = await listCams();
    id = known.some((d) => d.label) ? pickDevice(known, lsGet(DEVICE_KEY)) : null;
  }
  const gum = (c) => navigator.mediaDevices.getUserMedia(c);
  let s;
  try {
    s = await gum(captureConstraints(settings, id));
  } catch (e) {
    if (!id || (e.name !== 'OverconstrainedError' && e.name !== 'NotFoundError')) throw e;
    lsDel(DEVICE_KEY); // stale device id
    s = await gum(captureConstraints(settings, null));
  }
  stream = s;
  el.video.srcObject = s;
  try { await el.video.play(); } catch (e) { if (e.name !== 'AbortError') throw e; }
  const track = s.getVideoTracks()[0];
  const ts = track.getSettings ? track.getSettings() : {};
  currentDeviceId = ts.deviceId || null;
  if (currentDeviceId) lsSet(DEVICE_KEY, currentDeviceId);
  mirrored = ts.facingMode === 'user';
  el.video.classList.toggle('mirror', mirrored);
  track.onended = () => { if (running && visible) recoverStream(); };
  await refreshPicker(); // labels are only available after permission is granted
  resizeOverlay();
}

async function recoverStream() {
  try { await openStream(currentDeviceId || undefined); } catch (e) { showError(cameraError(e)); }
}

function cameraError(e) {
  switch (e && e.name) {
    case 'NotAllowedError': case 'PermissionDeniedError': return 'Camera permission denied. Allow camera access for this site in the browser settings.';
    case 'NotFoundError': case 'OverconstrainedError': return 'No matching camera found on this device.';
    case 'NotReadableError': case 'AbortError': return 'The camera is in use by another app.';
    case 'SecurityError': return 'The browser blocked camera access on this page (HTTPS required).';
    default: return 'Could not start the camera.';
  }
}

// Resolves once the frame was sent (or skipped).
function captureAndSend() {
  if (!running || !stream || el.video.readyState < 2 || !ws || ws.readyState !== WebSocket.OPEN) return Promise.resolve();
  const inFlight = encoding && Date.now() - encodeSince < 5000;
  if (shouldSkipFrame(ws.bufferedAmount, inFlight)) return Promise.resolve();
  const vw = el.video.videoWidth;
  const vh = el.video.videoHeight;
  if (!vw || !vh) return Promise.resolve();
  const w = Math.min(settings.capture_width, vw);
  const hh = Math.round(vh * w / vw);
  capCanvas = capCanvas || document.createElement('canvas');
  if (capCanvas.width !== w || capCanvas.height !== hh) { capCanvas.width = w; capCanvas.height = hh; }
  capCanvas.getContext('2d').drawImage(el.video, 0, 0, w, hh);
  encoding = true;
  encodeSince = Date.now();
  return new Promise((resolve) => {
    try {
      capCanvas.toBlob((blob) => {
        encoding = false;
        if (blob && ws && ws.readyState === WebSocket.OPEN && !shouldSkipFrame(ws.bufferedAmount, false)) {
          ws.send(blob);
          lastFrameAt = Date.now();
          sent.push(lastFrameAt);
        }
        resolve();
      }, 'image/jpeg', settings.jpeg_quality);
    } catch { encoding = false; resolve(); }
  });
}

// ---------- loops ----------
function previewTick(gen) {
  if (gen !== loopGen || !running) return;
  captureAndSend();
  previewTimer = setTimeout(() => previewTick(gen), sendIntervalMs(settings, effectiveMode));
}

async function detectTick(gen) {
  if (gen !== loopGen || effectiveMode !== 'local' || !model || !running) return;
  if (el.video.readyState < 2) { detectTimer = setTimeout(() => detectTick(gen), 100); return; }
  const start = performance.now();
  try {
    const preds = await model.detect(el.video, 20, 0.3);
    if (gen !== loopGen) return;
    const ms = performance.now() - start;
    lastInferMs = Math.round(ms);
    const fw = el.video.videoWidth;
    const fh = el.video.videoHeight;
    pending = preds.map((p) => ({ label: p.class, score: p.score, bbox: p.bbox }));
    pendingAt = Date.now();
    drawBoxes(pending, fw, fh);
    allSamples.push(ms);
    const now = Date.now();
    const wantSend = ws && ws.readyState === WebSocket.OPEN && (detectionsChanged(lastSentDets, pending, fw, fh) || now - lastSentDetsAt >= 2000);
    // The frame goes out before the detections so the server's event snapshot is current.
    if (visibleDets().length && now - lastFrameAt > 400) { await captureAndSend(); if (gen !== loopGen) return; }
    if (wantSend && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'detections', detections: pending, frame_w: fw, frame_h: fh, infer_ms: lastInferMs }));
      lastSentDets = pending.map((d) => ({ label: d.label, score: d.score, bbox: [...d.bbox] }));
      lastSentDetsAt = now;
    }
    if (settings.mode === 'auto' && !autoDecided) {
      const slow = inferenceTooSlow(allSamples);
      if (slow) { await switchToServer('inference too slow', true); return; }
      if (slow === false) autoDecided = true;
    }
  } catch (e) {
    console.error('detection failed', e);
    if (settings.mode === 'auto' && !autoDecided) { await switchToServer('detection failed', true); return; }
  }
  if (gen === loopGen && running && effectiveMode === 'local') {
    detectTimer = setTimeout(() => detectTick(gen), Math.max(0, 1000 / settings.detect_fps - (performance.now() - start)));
  }
}

function pauseLoops() {
  loopGen++;
  clearTimeout(detectTimer); detectTimer = null;
  clearTimeout(previewTimer); previewTimer = null;
}
function resumeLoops() {
  if (!running || !visible) return;
  pauseLoops();
  const gen = loopGen;
  if (effectiveMode === 'local') detectTimer = setTimeout(() => detectTick(gen), 0);
  previewTimer = setTimeout(() => previewTick(gen), 0);
}

// ---------- detection mode ----------
async function loadModel() {
  if (model) return true;
  if (typeof tf === 'undefined' || typeof cocoSsd === 'undefined') return false; // blocked or not loaded
  try {
    if (!(await tf.setBackend('webgl'))) return false; // cpu/wasm are too slow
    await tf.ready();
    model = await cocoSsd.load({ modelUrl: '/static/vendor/ssdlite_mobilenet_v2/model.json' });
    return true;
  } catch (e) {
    console.warn('model load failed', e);
    return false;
  }
}
function disposeModel() {
  if (model && model.dispose) model.dispose();
  model = null;
}

function sendJson(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

async function switchToServer(reason, auto = false) {
  if (effectiveMode === 'server') return;
  autoFellBack = auto;
  effectiveMode = 'server';
  disposeModel();
  setModeBadge('server');
  sendJson({ type: 'mode', mode: 'server' });
  if (reason) toast(`Using server detection: ${reason}`, 'warn');
  resumeLoops();
}

async function tryLocal() {
  if (effectiveMode === 'local') return;
  if (!(await loadModel())) {
    if (canFallbackToServer(settings.mode)) { await switchToServer('on-device model unavailable', true); return; }
    showError('The on-device model could not be loaded. Choose server or auto mode in the dashboard settings.');
    return; // explicit local mode stays idle
  }
  if (!running) return;
  effectiveMode = 'local';
  autoFellBack = false;
  autoDecided = false;
  allSamples = [];
  setModeBadge('local');
  sendJson({ type: 'mode', mode: 'local' });
  resumeLoops();
}

function applyMode() {
  return settings.mode === 'server' ? switchToServer('') : tryLocal();
}

// ---------- websocket ----------
function connect() {
  if (!running || (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))) return;
  clearTimeout(reconnectTimer);
  setStatus('connecting', 'warn');
  const sock = new WebSocket(wsUrl('/ws/camera'));
  ws = sock;
  sock.binaryType = 'arraybuffer';
  sock.onopen = () => {
    setStatus('connected', 'ok');
    // CONTRACT-GAP: an empty name lets the server use the device name for paired devices.
    sock.send(JSON.stringify({ type: 'hello', camera_id: cameraId, name: cameraName }));
    if (effectiveMode !== 'starting') sock.send(JSON.stringify({ type: 'mode', mode: effectiveMode }));
    clearInterval(pingTimer);
    pingTimer = setInterval(() => sendJson({ type: 'ping' }), 20000);
    clearTimeout(stableTimer);
    stableTimer = setTimeout(() => { attempt = 0; }, 30000); // reset the backoff only after a stable connection
  };
  sock.onmessage = (e) => {
    if (typeof e.data !== 'string') return;
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === 'config') onConfig(m);
    else if (m.type === 'detections' && effectiveMode === 'server') {
      pending = Array.isArray(m.detections) ? m.detections : [];
      pendingAt = Date.now();
      lastInferMs = Math.round(m.infer_ms || 0);
      drawBoxes(pending, m.frame_w, m.frame_h);
      updateHud();
    } else if (m.type === 'error') toast(`Server: ${m.message}`, 'err');
  };
  sock.onclose = (e) => {
    if (ws !== sock) return;
    clearInterval(pingTimer);
    clearTimeout(stableTimer);
    ws = null;
    if (!running) return;
    const why = closeReason(e.code);
    if (why === 'revoked') { handleRevoked(); return; }
    if (why === 'replaced') { stopCamera(); show(el.replaced); return; }
    if (why === 'setup') { location.assign('/welcome'); return; }
    if (why === 'rate_limited') attempt = Math.max(attempt, 4);
    setStatus('reconnecting', 'warn');
    if (visible) reconnectTimer = setTimeout(connect, backoffDelay(attempt++));
  };
  sock.onerror = () => { /* onclose follows */ };
}

// A password change rotates this session's cookie and closes sockets with 4401: probe before giving up.
async function handleRevoked() {
  let stillAdmin = false;
  try { await api('/api/me', { redirectOn401: false }); stillAdmin = true; } catch { /* device principal or signed out */ }
  if (!running) return;
  if (stillAdmin) { attempt = 0; connect(); return; }
  stopCamera(); hide(el.setup); show(el.revoked); setStatus('revoked', 'err');
}

function onConfig(m) {
  const first = configWaiter;
  const prevMode = settings.mode;
  settings = { ...settings, ...m.settings };
  if (typeof m.camera_id === 'string' && m.camera_id) cameraId = m.camera_id; // authoritative
  syncThemeFromSettings(m.settings);
  if (first) { configWaiter = null; first(); return; }
  if (!modeChosen) return;
  if (settings.mode !== prevMode) autoFellBack = false;
  if (autoFellBack && settings.mode === 'auto') return; // do not retry the local model after an auto fallback
  applyMode();
}

// ---------- lifecycle ----------
async function start() {
  hide(el.error); hide(el.revoked); hide(el.replaced);
  autoFellBack = false;
  cameraName = el.name.value.trim();
  lsSet(NAME_KEY, cameraName);
  el.start.disabled = true;
  running = true;
  attempt = 0;
  try {
    await openStream(); // first: it needs the tap's user activation for the permission prompt
  } catch (e) {
    running = false;
    el.start.disabled = false;
    showError(cameraError(e));
    return;
  }
  hide(el.setup);
  show(el.controls);
  el.start.disabled = false;
  acquireWakeLock();
  const configured = new Promise((res) => { configWaiter = res; setTimeout(res, 4000); });
  connect();
  await configured;
  configWaiter = null;
  if (!running) return;
  modeChosen = true;
  await applyMode();
  resumeLoops();
}

function stopCamera() {
  running = false;
  modeChosen = false;
  effectiveMode = 'starting';
  pauseLoops();
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  clearTimeout(stableTimer);
  releaseWakeLock();
  if (ws) { const s = ws; ws = null; s.close(1000, 'stop'); }
  stopStream();
  disposeModel();
  pending = null;
  clearOverlay();
  updateHud();
  setStatus('stopped');
  setModeBadge('starting');
  hide(el.controls);
  show(el.setup);
  if (dimmed) toggleDim();
}

function toggleDim() {
  dimmed = !dimmed;
  el.dimLayer.classList.toggle('hidden', !dimmed);
  el.dimBtn.textContent = dimmed ? 'wake' : 'dim';
}

async function switchCamera() {
  if (cams.length < 2) return;
  const i = cams.findIndex((d) => d.deviceId === currentDeviceId);
  await changeDevice(cams[(i + 1) % cams.length].deviceId);
}

async function changeDevice(id) {
  try { await openStream(id); hide(el.error); } catch (e) { showError(cameraError(e)); recoverStream(); }
}

document.addEventListener('visibilitychange', async () => {
  visible = !document.hidden;
  if (!running) return;
  if (!visible) { pauseLoops(); clearTimeout(reconnectTimer); setStatus('paused', 'warn'); return; }
  if (!wakeLock) acquireWakeLock(); // the browser releases the lock while hidden
  const live = stream && stream.getVideoTracks().some((t) => t.readyState === 'live');
  if (!live) await recoverStream(); else el.video.play().catch(() => {});
  if (!ws) { attempt = 0; connect(); } else setStatus(ws.readyState === WebSocket.OPEN ? 'connected' : 'connecting', ws.readyState === WebSocket.OPEN ? 'ok' : 'warn');
  resumeLoops();
});

window.addEventListener('pagehide', () => { if (running) stopCamera(); });
window.addEventListener('resize', resizeOverlay);
window.addEventListener('orientationchange', () => setTimeout(resizeOverlay, 150));
el.video.addEventListener('loadedmetadata', resizeOverlay);
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', () => { if (running) refreshPicker(); });
}

el.start.addEventListener('click', start);
el.stopBtn.addEventListener('click', stopCamera);
el.dimBtn.addEventListener('click', toggleDim);
el.dimLayer.addEventListener('click', toggleDim);
el.switchBtn.addEventListener('click', switchCamera);
el.picker.addEventListener('change', () => changeDevice(el.picker.value));
el.name.addEventListener('change', () => {
  cameraName = el.name.value.trim();
  lsSet(NAME_KEY, cameraName); // sent with the next hello (the server expects exactly one per connection)
});

loadIdentity();
if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
  show(el.insecure);
} else {
  show(el.setup);
}
setModeBadge('starting');
