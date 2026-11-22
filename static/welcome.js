const PIN_RE = /^[0-9]{4,12}$/;

const steps = document.querySelectorAll('.welcome-step');
const progressDots = document.querySelectorAll('.progress-dot');
const progressLabels = document.querySelectorAll('.progress-labels span');
const welcomeError = document.getElementById('welcomeError');

const pinInput = document.getElementById('pin');
const pinConfirmInput = document.getElementById('pinConfirm');
const pinError = document.getElementById('pinError');
const pinConfirmError = document.getElementById('pinConfirmError');
const pinHint = document.getElementById('pinHint');
const pinNextBtn = document.getElementById('pinNextBtn');

let currentStep = 1;

function showStep(step) {
  steps.forEach(s => {
    const sNum = parseInt(s.dataset.step, 10);
    if (sNum === step) {
      s.hidden = false;
      s.classList.add('active');
    } else {
      s.hidden = true;
      s.classList.remove('active');
    }
  });
  progressDots.forEach(d => {
    const dNum = parseInt(d.dataset.step, 10);
    d.classList.toggle('active', dNum <= step);
    d.classList.toggle('completed', dNum < step);
  });
  progressLabels.forEach(l => {
    const lNum = parseInt(l.dataset.step, 10);
    l.classList.toggle('active', lNum === step);
    l.classList.toggle('completed', lNum < step);
  });
  document.querySelector('.welcome-progress').setAttribute('aria-valuenow', step);
  currentStep = step;
  welcomeError.classList.add('hidden');
  welcomeError.textContent = '';
}

function validatePinStep() {
  const pin = pinInput.value;
  const confirm = pinConfirmInput.value;
  let valid = true;

  pinError.textContent = '';
  pinConfirmError.textContent = '';
  pinInput.classList.remove('invalid');
  pinConfirmInput.classList.remove('invalid');

  if (!PIN_RE.test(pin)) {
    pinError.textContent = 'PIN must be 4–12 digits';
    pinInput.classList.add('invalid');
    valid = false;
  }
  if (confirm && pin !== confirm) {
    pinConfirmError.textContent = 'PINs do not match';
    pinConfirmInput.classList.add('invalid');
    valid = false;
  }
  pinNextBtn.disabled = !valid;
  return valid;
}

pinInput.addEventListener('input', () => {
  validatePinStep();
  pinHint.textContent = PIN_RE.test(pinInput.value) ? '✓ Valid' : '4–12 digits';
  pinHint.style.color = PIN_RE.test(pinInput.value) ? 'var(--ok)' : 'var(--muted)';
});
pinConfirmInput.addEventListener('input', validatePinStep);

document.querySelectorAll('[data-action="next"]').forEach(btn => {
  btn.addEventListener('click', async () => {
    if (currentStep === 1) {
      showStep(2);
    } else if (currentStep === 2) {
      if (!validatePinStep()) return;
      showStep(3);
    } else if (currentStep === 3) {
      await submitSetup();
    }
  });
});

document.querySelectorAll('[data-action="prev"]').forEach(btn => {
  btn.addEventListener('click', () => {
    if (currentStep > 1) showStep(currentStep - 1);
  });
});

document.querySelectorAll('.toggle-visibility').forEach(btn => {
  btn.addEventListener('click', () => {
    const wrapper = btn.closest('.input-wrapper');
    const input = wrapper.querySelector('input');
    const isPassword = input.type === 'password';
    input.type = isPassword ? 'text' : 'password';
    btn.setAttribute('aria-pressed', isPassword ? 'true' : 'false');
    btn.querySelector('.icon-eye').classList.toggle('hidden', isPassword);
    btn.querySelector('.icon-eye-off').classList.toggle('hidden', !isPassword);
  });
});

function getSelectedLabels() {
  const labels = [];
  const form = document.getElementById('prefsForm');
  if (form.group_people?.checked) labels.push('person');
  if (form.group_vehicles?.checked) labels.push('car', 'truck', 'bus', 'motorcycle', 'bicycle');
  if (form.group_animals?.checked) labels.push('cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear');
  return labels;
}

async function submitSetup() {
  const pin = pinInput.value;
  const pinConfirm = pinConfirmInput.value;
  const retentionDays = parseInt(document.getElementById('retention').value, 10);
  const mode = document.querySelector('input[name="mode"]:checked')?.value || 'auto';
  const labels = getSelectedLabels();

  if (pin !== pinConfirm) {
    showError('PINs do not match');
    return;
  }
  if (!PIN_RE.test(pin)) {
    showError('Invalid PIN');
    return;
  }
  if (labels.length === 0) {
    showError('Select at least one detection category');
    return;
  }

  const nextBtn = document.querySelector('[data-step="3"] [data-action="next"]');
  nextBtn.disabled = true;
  nextBtn.textContent = 'Setting up…';

  try {
    const res = await fetch('/api/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ pin, pin_confirm: pinConfirm, retention_days: retentionDays, mode, labels })
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      showStep(4);
    } else if (res.status === 429) {
      showError(data.detail || 'Too many attempts. Please wait a moment.');
      nextBtn.disabled = false;
      nextBtn.textContent = 'Next';
    } else if (res.status === 409) {
      showError('Already set up');
      nextBtn.disabled = false;
      nextBtn.textContent = 'Next';
    } else {
      showError(data.detail || 'Setup failed. Please try again.');
      nextBtn.disabled = false;
      nextBtn.textContent = 'Next';
    }
  } catch (err) {
    showError('Network error. Please check your connection.');
    nextBtn.disabled = false;
    nextBtn.textContent = 'Next';
  }
}

function showError(msg) {
  welcomeError.textContent = msg;
  welcomeError.classList.remove('hidden');
}
