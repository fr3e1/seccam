// Settings drawer: Detection, Appearance, Account, Devices, Update. All server strings go through textContent.
import { api, ApiError } from './api.js';
import { $, h, toast, fmtRelative, copyText } from './ui.js';
import { THEMES, saveTheme } from './theme.js';
import { ALL_LABELS, labelCategory } from './camera-logic.js';

const TABS = [
  ['detection', 'Detection'], ['appearance', 'Appearance'], ['account', 'Account'],
  ['devices', 'Devices'], ['update', 'Update'],
];
const MODES = [
  ['auto', 'auto: phone tries on-device detection, falls back to the server'],
  ['local', 'local: detection runs on the phone (TensorFlow.js)'],
  ['server', 'server: the phone streams frames, the server detects (YOLOX-Nano)'],
];

let settings = null;
let activeTab = 'detection';
let cleanup = null;
let updateJob = null;

const drawer = () => $('#settings-drawer');
const panel = () => $('#settings-panel');

// api() with redirectOn401:false: wrong-password answers are 401 too, so only "unauthorized" means signed out.
async function call(path, opts = {}) {
  try {
    return await api(path, { redirectOn401: false, ...opts });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401 && e.code === 'unauthorized') {
      location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search));
    }
    throw e;
  }
}

function fail(e, fallback = 'Request failed') {
  toast(e instanceof ApiError ? (e.detail || fallback) : fallback, 'err');
}

function section(title, ...kids) {
  return h('div', { class: 'section' }, h('h3', {}, title), ...kids);
}

function field(label, control, hint) {
  return h('label', { class: 'field' }, h('span', { class: 'field-label' }, label), control,
    hint ? h('span', { class: 'field-hint' }, hint) : null);
}

function textInput(type, extra = {}) {
  return h('input', { class: 'input', type, autocomplete: 'off', ...extra });
}

function pwInput(label, autocomplete = 'current-password') {
  const input = textInput('password', { autocomplete });
  return { input, el: field(label, input) };
}

function range(label, min, max, step, value, fmt = (v) => String(v)) {
  const input = h('input', { type: 'range', min, max, step, value });
  const out = h('output', {}, fmt(Number(value)));
  input.addEventListener('input', () => { out.textContent = fmt(Number(input.value)); });
  return { input, el: h('div', { class: 'field' }, h('span', { class: 'field-label' }, label), h('div', { class: 'range-row' }, input, out)) };
}

function copyRow(text, label = 'copy') {
  const btn = h('button', { class: 'btn btn-sm', type: 'button' }, label);
  btn.addEventListener('click', async () => toast((await copyText(text)) ? 'Copied' : 'Copy failed', 'ok'));
  return h('div', { class: 'stack' }, h('code', { class: 'secret' }, text), h('div', {}, btn));
}

// ---------- Detection ----------
function renderDetection(root) {
  const s = settings;
  const boxes = ALL_LABELS.map((l) => {
    const input = h('input', { type: 'checkbox', value: l, checked: (s.labels || []).includes(l) });
    return { input, el: h('label', { class: 'check' }, input, h('span', { class: `lbl lbl-${labelCategory(l)}` }, l)) };
  });
  const minScore = range('min score', 0.1, 0.95, 0.01, s.min_score, (v) => v.toFixed(2));
  const cooldown = textInput('number', { min: 0, max: 3600, step: 1, value: s.cooldown_s, inputmode: 'numeric' });
  const preview = range('preview fps', 0.2, 15, 0.1, s.preview_fps, (v) => v.toFixed(1));
  const detect = range('detect fps', 0.2, 10, 0.1, s.detect_fps, (v) => v.toFixed(1));
  const mode = h('select', { class: 'select' }, MODES.map(([v, d]) => h('option', { value: v, selected: s.mode === v }, d)));
  const retention = range('retention (days)', 1, 365, 1, s.retention_days);
  const quality = range('jpeg quality', 0.3, 0.95, 0.05, s.jpeg_quality, (v) => v.toFixed(2));
  const width = range('capture width (px)', 320, 1920, 10, s.capture_width);
  const save = h('button', { class: 'btn btn-primary', type: 'button' }, 'save');
  save.addEventListener('click', async () => {
    const cd = Number(cooldown.value);
    if (!Number.isInteger(cd) || cd < 0 || cd > 3600) { toast('Cooldown must be a whole number of seconds, 0 to 3600', 'err'); return; }
    save.disabled = true;
    try {
      settings = await call('/api/settings', {
        method: 'PUT',
        body: {
          labels: boxes.filter((b) => b.input.checked).map((b) => b.input.value),
          min_score: Number(minScore.input.value), cooldown_s: cd,
          preview_fps: Number(preview.input.value), detect_fps: Number(detect.input.value),
          mode: mode.value, retention_days: Number(retention.input.value),
          jpeg_quality: Number(quality.input.value), capture_width: Number(width.input.value),
        },
      });
      toast('Settings saved', 'ok');
    } catch (e) { fail(e, 'Could not save settings'); } finally { save.disabled = false; }
  });
  root.append(
    section('labels', h('div', { class: 'label-grid' }, boxes.map((b) => b.el))),
    section('thresholds', minScore.el, field('cooldown (seconds)', cooldown), preview.el, detect.el),
    section('detection mode', field('mode', mode)),
    section('storage', retention.el),
    section('camera capture', quality.el, width.el, h('p', { class: 'field-hint' }, 'Applies to cameras the next time they start.')),
    save,
  );
}

// ---------- Appearance ----------
function renderAppearance(root) {
  const current = document.documentElement.dataset.theme;
  const list = h('div', { class: 'theme-list' });
  for (const t of THEMES) {
    const btn = h('button', { class: 'theme-item theme-scope', type: 'button', 'aria-pressed': t.id === current ? 'true' : 'false', dataset: { theme: t.id } },
      h('span', {}, t.name), h('span', { class: 'swatch', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'), h('i'), h('i')));
    btn.addEventListener('click', async () => {
      try {
        const applied = await saveTheme(t.id);
        if (settings) settings.theme = applied;
        list.querySelectorAll('.theme-item').forEach((b) => b.setAttribute('aria-pressed', b.dataset.theme === applied ? 'true' : 'false'));
      } catch (e) { fail(e, 'Theme applied here but could not be saved'); }
    });
    list.append(btn);
  }
  root.append(section('theme', list));
}

// ---------- Account ----------
async function renderAccount(root) {
  let me;
  try { me = await call('/api/me'); } catch (e) { root.append(h('p', { class: 'error-text' }, 'Could not load account')); return; }
  root.append(h('p', { class: 'muted' }, 'signed in as ', h('strong', {}, me.username)));

  const cur = pwInput('current password');
  const nw = pwInput('new password (10+ characters)', 'new-password');
  const cf = pwInput('confirm new password', 'new-password');
  const chg = h('button', { class: 'btn btn-primary', type: 'button' }, 'change password');
  chg.addEventListener('click', async () => {
    if (nw.input.value.length < 10) { toast('New password needs at least 10 characters', 'err'); return; }
    if (nw.input.value !== cf.input.value) { toast('Passwords do not match', 'err'); return; }
    chg.disabled = true;
    try {
      await call('/api/account/password', { method: 'POST', body: { current_password: cur.input.value, new_password: nw.input.value } });
      cur.input.value = nw.input.value = cf.input.value = '';
      toast('Password changed, other sessions signed out', 'ok');
    } catch (e) { fail(e, 'Could not change password'); } finally { chg.disabled = false; }
  });
  root.append(section('password', cur.el, nw.el, cf.el, chg));

  const twofa = h('div', {});
  root.append(section('two-factor authentication', twofa));
  const showTwofa = (enabled) => { twofa.replaceChildren(); (enabled ? twofaDisable : twofaStart)(twofa, showTwofa); };
  showTwofa(me.totp_enabled);

  const others = h('button', { class: 'btn', type: 'button' }, 'sign out other sessions');
  others.addEventListener('click', async () => {
    try {
      const r = await call('/api/sessions/revoke-others', { method: 'POST' });
      toast(`Signed out ${r.revoked} other session(s)`, 'ok');
    } catch (e) { fail(e); }
  });
  const out = h('button', { class: 'btn btn-danger', type: 'button' }, 'sign out');
  out.addEventListener('click', signOut);
  root.append(section('sessions', h('div', { class: 'row wrap' }, others, out)));
}

export async function signOut() {
  try { await api('/api/logout', { method: 'POST', redirectOn401: false }); } catch { /* leaving anyway */ }
  location.assign('/login');
}

function twofaStart(root, refresh) {
  const pw = pwInput('password');
  const go = h('button', { class: 'btn', type: 'button' }, 'set up 2FA');
  const step2 = h('div', {});
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const r = await call('/api/account/totp/setup', { method: 'POST', body: { password: pw.input.value } });
      pw.input.value = '';
      const code = textInput('text', { inputmode: 'numeric', maxlength: 6, placeholder: '123456' });
      const enable = h('button', { class: 'btn btn-primary', type: 'button' }, 'enable');
      enable.addEventListener('click', async () => {
        enable.disabled = true;
        try {
          const e = await call('/api/account/totp/enable', { method: 'POST', body: { code: code.value.trim() } });
          showRecovery(root, e.recovery_codes, refresh);
        } catch (err) { fail(err, 'Could not enable 2FA'); enable.disabled = false; }
      });
      step2.replaceChildren(
        h('p', { class: 'muted' }, 'Add this secret to your authenticator app (or paste the URI), then enter a code.'),
        copyRow(r.secret, 'copy secret'), copyRow(r.otpauth_uri, 'copy uri'),
        field('code from the app', code), enable);
    } catch (e) { fail(e, 'Could not start 2FA setup'); } finally { go.disabled = false; }
  });
  root.append(h('p', { class: 'muted' }, '2FA is off.'), pw.el, go, step2);
}

function showRecovery(root, codes, refresh) {
  const text = codes.join('\n');
  const done = h('button', { class: 'btn btn-primary', type: 'button' }, 'I saved them');
  done.addEventListener('click', () => refresh(true));
  root.replaceChildren(
    h('p', { class: 'ok' }, '2FA is on. Save these recovery codes now; they are shown once.'),
    h('div', { class: 'codes' }, codes.map((c) => h('code', {}, c))),
    h('div', { class: 'row wrap' }, (() => {
      const b = h('button', { class: 'btn', type: 'button' }, 'copy codes');
      b.addEventListener('click', async () => toast((await copyText(text)) ? 'Copied' : 'Copy failed', 'ok'));
      return b;
    })(), done));
}

function twofaDisable(root, refresh) {
  const pw = pwInput('password');
  const code = textInput('text', { placeholder: '123456 or recovery code' });
  const btn = h('button', { class: 'btn btn-danger', type: 'button' }, 'disable 2FA');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await call('/api/account/totp/disable', { method: 'POST', body: { password: pw.input.value, code: code.value.trim() } });
      toast('2FA disabled', 'ok');
      refresh(false);
    } catch (e) { fail(e, 'Could not disable 2FA'); btn.disabled = false; }
  });
  root.append(h('p', { class: 'ok' }, '2FA is on.'), pw.el, field('code or recovery code', code), btn);
}

// ---------- Devices ----------
export function shellQuote(s) {
  return /^[A-Za-z0-9_.@%+=:,\/-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'";
}

function renderDevices(root) {
  const name = textInput('text', { maxlength: 64, placeholder: 'Garage' });
  const create = h('button', { class: 'btn btn-primary', type: 'button' }, 'new pairing code');
  const result = h('div', { class: 'stack' });
  const list = h('div', {});
  let timer = null;
  cleanup = () => { if (timer) clearInterval(timer); };

  create.addEventListener('click', async () => {
    create.disabled = true;
    try {
      const nm = name.value.trim();
      const r = await call('/api/devices/pairing', { method: 'POST', body: nm ? { name: nm } : {} });
      const left = h('span', { class: 'muted' });
      const tick = () => {
        const s = Math.max(0, Math.round((r.expires_at - Date.now()) / 1000));
        left.textContent = s > 0 ? `expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : 'expired';
        if (s === 0 && timer) { clearInterval(timer); timer = null; }
      };
      if (timer) clearInterval(timer);
      timer = setInterval(tick, 1000);
      tick();
      const origin = location.origin;
      const cmd = `python3 seccam_agent.py pair --server ${origin} --code ${r.code} --name ${shellQuote(nm || 'Camera')}`;
      result.replaceChildren(
        h('div', { class: 'big-code' }, r.code), left,
        h('p', { class: 'muted' }, 'Browser camera: open this page and enter the code.'), copyRow(`${origin}/pair`, 'copy url'),
        h('p', { class: 'muted' }, 'Agent (Raspberry Pi, PC):'), copyRow(cmd, 'copy command'));
    } catch (e) { fail(e, 'Could not create a pairing code'); } finally { create.disabled = false; }
  });

  async function load() {
    let devices;
    try { devices = (await call('/api/devices')).devices; } catch { list.replaceChildren(h('p', { class: 'error-text' }, 'Could not load devices')); return; }
    list.replaceChildren(...(devices.length ? devices.map((d) => deviceRow(d, load)) : [h('p', { class: 'muted' }, 'No paired devices yet.')]));
  }
  root.append(section('pair a camera', field('name (optional)', name), create, result), section('paired devices', list));
  load();
}

function deviceRow(d, reload) {
  const nameEl = h('span', { class: 'dev-name' }, d.name);
  const status = h('span', { class: d.connected ? 'badge badge-ok' : 'badge' }, d.connected ? 'connected' : 'offline');
  const seen = h('span', { class: 'muted' }, d.last_seen_at ? fmtRelative(d.last_seen_at) : 'never seen');
  const rename = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, 'rename');
  const revoke = h('button', { class: 'btn btn-danger btn-sm', type: 'button' }, 'revoke');
  const row = h('div', { class: 'dev-row' }, nameEl, h('span', { class: 'badge' }, d.kind), status, seen, rename, revoke);
  rename.addEventListener('click', () => {
    const input = textInput('text', { maxlength: 64, value: d.name });
    const ok = h('button', { class: 'btn btn-primary btn-sm', type: 'button' }, 'save');
    const no = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, 'cancel');
    no.addEventListener('click', reload);
    ok.addEventListener('click', async () => {
      const nm = input.value.trim();
      if (!nm || nm.length > 64) { toast('Name must be 1 to 64 characters', 'err'); return; }
      try { await call(`/api/devices/${encodeURIComponent(d.id)}`, { method: 'PATCH', body: { name: nm } }); reload(); } catch (e) { fail(e); }
    });
    row.replaceChildren(input, ok, no);
  });
  revoke.addEventListener('click', async () => {
    if (!confirm(`Revoke "${d.name}"? It will be disconnected and must be paired again.`)) return;
    try { await call(`/api/devices/${encodeURIComponent(d.id)}`, { method: 'DELETE' }); toast('Device revoked', 'ok'); reload(); } catch (e) { fail(e); }
  });
  return row;
}

// ---------- Update ----------
const ACTIVE = new Set(['requested', 'downloading', 'verifying', 'installing', 'restarting', 'health_check']);
const POLL_MS = 2000;

function renderUpdate(root) {
  const box = h('div', {});
  root.append(box);
  cleanup = () => { if (updateJob) updateJob.paint = null; };
  let info = null;

  const paint = () => {
    if (!info) return;
    const st = info.status || {};
    const kids = [
      h('p', {}, 'current version: ', h('strong', {}, info.current_version)),
      h('p', {}, 'latest version: ', h('strong', {}, info.latest ? info.latest.version : 'unknown'),
        info.latest && info.latest.published_at ? h('span', { class: 'muted' }, ` (${info.latest.published_at})`) : null),
    ];
    if (info.checked_at) kids.push(h('p', { class: 'muted' }, `checked ${fmtRelative(info.checked_at)}`));
    if (info.error) kids.push(h('p', { class: 'error-text' }, info.error));
    if (info.latest && info.latest.notes) kids.push(h('pre', { class: 'release-notes' }, info.latest.notes));
    if (info.latest && info.latest.html_url && /^https:\/\//.test(info.latest.html_url)) {
      kids.push(h('p', {}, h('a', { href: info.latest.html_url, target: '_blank', rel: 'noopener noreferrer' }, 'release page')));
    }
    if (!info.enabled) kids.push(h('p', { class: 'note' }, 'One-click updates need the systemd install (SECCAM_UPDATE_MODE=systemd). See the README to update manually.'));
    if (st.state && st.state !== 'idle') {
      kids.push(h('p', { class: ACTIVE.has(st.state) ? 'warn' : st.state === 'succeeded' ? 'ok' : 'err' },
        `status: ${st.state}`, st.message ? ` - ${st.message}` : ''));
    }
    const check = h('button', { class: 'btn', type: 'button' }, 'check now');
    check.addEventListener('click', async () => {
      check.disabled = true;
      try { info = await call('/api/update?refresh=1'); paint(); } catch (e) { fail(e); check.disabled = false; }
    });
    const actions = h('div', { class: 'row wrap' }, check);
    const busy = updateJob !== null || ACTIVE.has(st.state);
    if (info.enabled && info.update_available && info.latest && !busy) {
      const pw = pwInput('password to confirm the update');
      const go = h('button', { class: 'btn btn-primary', type: 'button' }, `update to ${info.latest.version}`);
      go.addEventListener('click', () => {
        if (!pw.input.value) { toast('Enter your password to confirm', 'err'); pw.input.focus(); return; }
        applyUpdate(info.latest.version, pw.input.value, info.current_version, go, (i) => { info = i; paint(); });
      });
      kids.push(actions, h('div', { class: 'section' }, pw.el, go));
    } else {
      kids.push(actions);
    }
    box.replaceChildren(...kids);
  };
  if (updateJob) updateJob.paint = (i) => { info = i; paint(); };
  box.replaceChildren(h('span', { class: 'spinner' }));
  call('/api/update').then((i) => { info = i; paint(); }, (e) => { box.replaceChildren(h('p', { class: 'error-text' }, e instanceof ApiError ? e.detail : 'Could not load update info')); });
}

async function applyUpdate(version, password, oldVersion, btn, repaint) {
  btn.disabled = true;
  try {
    await call('/api/update/apply', { method: 'POST', body: { version, password } });
  } catch (e) { fail(e, 'Could not start the update'); btn.disabled = false; return; }
  updateJob = { paint: repaint };
  const deadline = Date.now() + 10 * 60 * 1000;
  try {
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      let info;
      try { info = await api('/api/update', { redirectOn401: false }); } catch { continue; } // the service is restarting
      if (updateJob && updateJob.paint) updateJob.paint(info);
      const state = info.status && info.status.state;
      if (state === 'succeeded') { await reloadWhenVersion(version); return; }
      if (state === 'rolled_back' || state === 'failed') { toast(info.status.message || `Update ${state}`, 'err', 8000); return; }
    }
    toast('Update is taking too long; check the service log', 'warn', 8000);
  } finally { updateJob = null; }
}

async function reloadWhenVersion(version) {
  for (let i = 0; i < 90; i++) {
    try {
      const r = await fetch('/api/health', { credentials: 'same-origin', cache: 'no-store' });
      const j = r.ok ? await r.json() : null;
      if (j && j.version === version) { location.reload(); return; }
    } catch { /* restarting */ }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  toast('Updated, but the new version did not report in; reload the page', 'warn', 8000);
}

// ---------- drawer ----------
async function renderTab() {
  if (cleanup) { cleanup(); cleanup = null; }
  const root = panel();
  root.replaceChildren();
  $('#settings-tabs').querySelectorAll('.tab').forEach((b) => {
    const on = b.dataset.tab === activeTab;
    b.classList.toggle('tab-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (activeTab === 'update') return renderUpdate(root);
  if (activeTab === 'devices') return renderDevices(root);
  if (activeTab === 'account') return renderAccount(root);
  if (!settings) {
    try { settings = await call('/api/settings'); } catch (e) { root.append(h('p', { class: 'error-text' }, 'Could not load settings')); return; }
  }
  if (activeTab === 'appearance') renderAppearance(root); else renderDetection(root);
}

export function setCurrentSettings(s) {
  settings = s;
  if (isSettingsOpen() && activeTab === 'appearance') renderTab();
}

export function isSettingsOpen() {
  const d = drawer();
  return Boolean(d && d.classList.contains('drawer-open'));
}

export function openSettings(tab = 'detection') {
  activeTab = TABS.some(([id]) => id === tab) ? tab : 'detection';
  const tabs = $('#settings-tabs');
  if (!tabs.children.length) {
    for (const [id, name] of TABS) {
      const b = h('button', { class: 'tab', type: 'button', role: 'tab', dataset: { tab: id } }, name);
      b.addEventListener('click', () => { activeTab = id; renderTab(); });
      tabs.append(b);
    }
  }
  drawer().classList.add('drawer-open');
  drawer().setAttribute('aria-hidden', 'false');
  $('#scrim').classList.remove('hidden');
  $('#settings-btn').setAttribute('aria-expanded', 'true');
  renderTab();
  $('#settings-close').focus();
}

export function closeSettings() {
  if (cleanup) { cleanup(); cleanup = null; }
  drawer().classList.remove('drawer-open');
  drawer().setAttribute('aria-hidden', 'true');
  $('#scrim').classList.add('hidden');
  $('#settings-btn').setAttribute('aria-expanded', 'false');
  $('#settings-btn').focus();
}
