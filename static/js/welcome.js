import { api, ApiError } from './api.js';
import { $, h } from './ui.js';
import { THEMES, DEFAULT_THEME, applyTheme } from './theme.js';

const LABELS = ['person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear'];
const DEFAULT_LABELS = new Set(['person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'cat', 'dog', 'bird']);
const LAST = 4;

const form = $('#setup-form');
const errorEl = $('#error');
const backBtn = $('#back');
const nextBtn = $('#next');
let step = 1;
let theme = DEFAULT_THEME;
let legacyAccepted = false;

function credKind() {
  const r = form.querySelector('input[name="cred"]:checked');
  return r ? r.value : 'token';
}

function render() {
  for (const s of form.querySelectorAll('[data-step]')) s.classList.toggle('hidden', Number(s.dataset.step) !== step);
  for (const d of document.querySelectorAll('[data-dot]')) d.classList.toggle('step-on', Number(d.dataset.dot) === step);
  backBtn.classList.toggle('hidden', step === 1);
  nextBtn.textContent = step === LAST ? 'Create account' : 'Next';
  errorEl.textContent = '';
}

function validate() {
  if (step === 1 && !$('#credential').value.trim()) {
    return credKind() === 'pin' ? 'Enter the legacy PIN.' : 'Enter the setup token.';
  }
  if (step === 2) {
    const u = $('#username').value.trim();
    const p = $('#password').value;
    if (!/^[A-Za-z0-9_.-]{3,32}$/.test(u)) return 'Username must be 3-32 chars: letters, digits, _ . -';
    if (p.length < 10) return 'Password must be at least 10 characters.';
    if (p !== $('#password-confirm').value) return 'Passwords do not match.';
  }
  if (step === 3 && !form.querySelector('#labels input:checked')) return 'Select at least one label.';
  return '';
}

function buildBody() {
  const body = {
    username: $('#username').value.trim(),
    password: $('#password').value,
    password_confirm: $('#password-confirm').value,
    retention_days: parseInt($('#retention').value, 10),
    mode: $('#mode').value,
    labels: [...form.querySelectorAll('#labels input:checked')].map((i) => i.value),
    theme,
  };
  body[credKind() === 'pin' ? 'legacy_pin' : 'setup_token'] = $('#credential').value.trim();
  return body;
}

async function finish() {
  nextBtn.disabled = true;
  try {
    await api('/api/setup', { method: 'POST', body: buildBody(), redirectOn401: false });
    location.assign('/');
    return;
  } catch (e) {
    if (e instanceof ApiError && e.code === 'already_setup') {
      location.assign('/login');
      return;
    }
    errorEl.textContent = e instanceof ApiError ? (e.detail || 'Setup failed.') : 'Network error. Try again.';
    if (e instanceof ApiError && (e.status === 401 || e.status === 429)) step = 1;
    else if (e instanceof ApiError && e.status === 422) step = /username|password/i.test(e.detail || '') ? 2 : 3;
    const msg = errorEl.textContent;
    render();
    errorEl.textContent = msg;
  } finally {
    nextBtn.disabled = false;
  }
}

form.addEventListener('submit', (ev) => {
  ev.preventDefault();
  const problem = validate();
  if (problem) { errorEl.textContent = problem; return; }
  if (step < LAST) { step += 1; render(); return; }
  finish();
});
backBtn.addEventListener('click', () => { if (step > 1) { step -= 1; render(); } });

for (const r of form.querySelectorAll('input[name="cred"]')) {
  r.addEventListener('change', () => {
    const pin = credKind() === 'pin';
    $('#cred-label').textContent = pin ? 'legacy PIN' : 'setup token';
    $('#cred-hint').textContent = pin ? 'The PIN you used with SecCam v1.' : 'Token is in <data_dir>/setup-token or the service log.';
    $('#credential').value = '';
  });
}

$('#labels').append(...LABELS.map((l) =>
  h('label', { class: 'check' }, h('input', { type: 'checkbox', value: l, checked: DEFAULT_LABELS.has(l) }), l)));

const themeList = $('#themes');
function renderThemes() {
  themeList.replaceChildren(...THEMES.map((t) =>
    h('button', {
      type: 'button', class: 'theme-item theme-scope', dataset: { theme: t.id }, 'aria-pressed': String(t.id === theme),
      onclick: () => { theme = applyTheme(t.id); renderThemes(); },
    }, h('span', {}, t.name), h('span', { class: 'swatch' }, h('i'), h('i'), h('i'), h('i'), h('i')))));
}
renderThemes();
render();

(async () => {
  try {
    const st = await api('/api/setup/status', { redirectOn401: false });
    if (!st.setup_required) { location.assign('/login'); return; }
    legacyAccepted = !!st.legacy_pin_accepted;
    $('#cred-choice').classList.toggle('hidden', !legacyAccepted);
  } catch { /* the POST will surface server errors */ }
})();
