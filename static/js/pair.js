import { api, ApiError, safeNext } from './api.js';
import { $ } from './ui.js';

const params = new URLSearchParams(location.search);
const next = safeNext(params.get('next') || '/camera');
const errorEl = $('#error');
const submit = $('#submit');
const nameInput = $('#name');

function guessDeviceName(ua = navigator.userAgent) {
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  if (/CrOS/.test(ua)) return 'Chromebook';
  if (/Windows/.test(ua)) return 'Windows PC';
  if (/Mac OS X|Macintosh/.test(ua)) return 'Mac';
  if (/Linux|X11/.test(ua)) return 'Linux PC';
  return 'Camera';
}

nameInput.value = guessDeviceName();
$('#admin-link').setAttribute('href', '/login?next=' + encodeURIComponent(next));
const prefill = params.get('code');
if (prefill) $('#code').value = prefill;

$('#pair-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  errorEl.textContent = '';
  const code = $('#code').value.trim();
  if (!code) {
    errorEl.textContent = 'Enter the pairing code.';
    return;
  }
  submit.disabled = true;
  try {
    await api('/api/pair', { method: 'POST', body: { code, name: nameInput.value.trim(), kind: 'browser' }, redirectOn401: false });
    location.assign(next);
    return;
  } catch (e) {
    errorEl.textContent = e instanceof ApiError ? (e.detail || 'Pairing failed.') : 'Network error. Try again.';
  }
  submit.disabled = false;
});
