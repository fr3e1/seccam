import { api, ApiError, safeNext } from './api.js';
import { $ } from './ui.js';

const form = $('#login-form');
const errorEl = $('#error');
const submit = $('#submit');
const totpField = $('#totp-field');
const totpInput = $('#totp');
const next = safeNext(new URLSearchParams(location.search).get('next'));

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  errorEl.textContent = '';
  const username = $('#username').value.trim();
  const password = $('#password').value;
  if (!username || !password) {
    errorEl.textContent = 'Enter a username and password.';
    return;
  }
  const body = { username, password };
  const totp = totpInput.value.trim();
  if (totp) body.totp = totp;
  submit.disabled = true;
  try {
    await api('/api/login', { method: 'POST', body, redirectOn401: false });
    location.assign(next);
    return;
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.code === 'totp_required') {
        totpField.classList.remove('hidden');
        totpInput.focus();
        errorEl.textContent = e.detail || 'Two-factor code required';
      } else if (e.code === 'setup_required') {
        location.assign('/welcome');
        return;
      } else {
        errorEl.textContent = e.detail || 'Sign in failed.';
      }
    } else {
      errorEl.textContent = 'Network error. Try again.';
    }
  }
  submit.disabled = false;
});
