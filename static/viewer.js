const ALL_LABELS = [
  'person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle',
  'cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear',
  'backpack', 'handbag', 'suitcase', 'umbrella'
];

const LABEL_COLOR_CLASS = {
  person: 'chip-person',
  car: 'chip-vehicle', truck: 'chip-vehicle', bus: 'chip-vehicle',
  motorcycle: 'chip-vehicle', bicycle: 'chip-vehicle',
  cat: 'chip-animal', dog: 'chip-animal', bird: 'chip-animal',
  horse: 'chip-animal', sheep: 'chip-animal', cow: 'chip-animal', bear: 'chip-animal',
  backpack: 'chip-other', handbag: 'chip-other', suitcase: 'chip-other', umbrella: 'chip-other'
};

const LABEL_DISPLAY = {
  person: 'Person', car: 'Car', truck: 'Truck', bus: 'Bus',
  motorcycle: 'Motorcycle', bicycle: 'Bicycle',
  cat: 'Cat', dog: 'Dog', bird: 'Bird', horse: 'Horse',
  sheep: 'Sheep', cow: 'Cow', bear: 'Bear',
  backpack: 'Backpack', handbag: 'Handbag', suitcase: 'Suitcase', umbrella: 'Umbrella'
};

const MODE_OPTIONS = [
  { value: 'auto', title: 'Auto', desc: 'Phone tries local detection first, falls back to server if slow or unavailable.' },
  { value: 'local', title: 'Local (Phone)', desc: 'All detection runs on the phone using TensorFlow.js. Best privacy, uses phone battery.' },
  { value: 'server', title: 'Server', desc: 'Phone streams frames; detection runs on the server (YOLOX-Nano). Lower phone battery, higher latency.' }
];

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.entries(v).forEach(([dk, dv]) => el.dataset[dk] = dv);
    else if (typeof v === 'boolean') { if (k in el) el[k] = v; else if (v) el.setAttribute(k, ''); }
    else if (v != null) el.setAttribute(k, v);
  });
  children.flat().forEach(child => {
    if (child == null) return;
    if (typeof child === 'string' || typeof child === 'number') el.appendChild(document.createTextNode(String(child)));
    else if (child instanceof Node) el.appendChild(child);
    else if (Array.isArray(child)) child.forEach(c => el.appendChild(c instanceof Node ? c : document.createTextNode(String(c))));
  });
  return el;
}

const PIN_RE = /^[0-9]{4,12}$/;
let ws = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
const cameras = new Map();
let settings = null;
let events = [];
let eventFilters = { labels: new Set(), cameraId: null };
let lastEventId = null;
let loadingMore = false;
let lastLoadCount = 0;
let notificationEnabled = false;
let notificationPermission = 'default';
let beepEnabled = false;
let audioCtx = null;
const lastNotifTime = new Map();
let unreadCount = 0;
let documentHidden = document.hidden;
let relativeTimeTimer = null;
let offlineCheckTimer = null;

const cameraGrid = document.getElementById('cameraGrid');
const eventsList = document.getElementById('eventsList');
const eventFiltersEl = document.getElementById('eventFilters');
const loadMoreBtn = document.getElementById('loadMoreBtn');
const connectionStatus = document.getElementById('connectionStatus');
const statusText = document.getElementById('statusText');
const notifBtn = document.getElementById('notifBtn');
const settingsBtn = document.getElementById('settingsBtn');
const settingsDrawer = document.getElementById('settingsDrawer');
const settingsClose = document.getElementById('settingsClose');
const settingsBody = document.getElementById('settingsBody');
const settingsSave = document.getElementById('settingsSave');
const drawerOverlay = document.getElementById('drawerOverlay');
const addCameraBtn = document.getElementById('addCameraBtn');
const addCameraPanel = document.getElementById('addCameraPanel');
const addCameraClose = document.getElementById('addCameraClose');
const addCameraUrl = document.getElementById('addCameraUrl');
const addCameraSetupLink = document.getElementById('addCameraSetupLink');
const eventModalOverlay = document.getElementById('eventModalOverlay');
const eventModalClose = document.getElementById('eventModalClose');
const eventModalCloseBtn = document.getElementById('eventModalCloseBtn');
const eventModalImage = document.getElementById('eventModalImage');
const eventModalDetails = document.getElementById('eventModalDetails');
const eventModalDelete = document.getElementById('eventModalDelete');
const toastContainer = document.getElementById('toastContainer');
const statsBar = document.getElementById('statsBar');
const logoutForm = document.getElementById('logoutForm');

function showToast(message, type = 'info') {
  const toast = h('div', { class: `toast toast-${type}` }, message);
  toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

function formatRelativeTime(ts) {
  const now = Date.now();
  const diff = now - ts;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

function formatAbsoluteTime(ts) {
  return new Date(ts).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}

function createChip(label) {
  return h('span', { class: `chip ${LABEL_COLOR_CLASS[label] || 'chip-other'}` }, LABEL_DISPLAY[label] || label);
}

function getLabelColor(label) {
  if (label === 'person') return '#f85149';
  if (['car','truck','bus','motorcycle','bicycle'].includes(label)) return '#2f81f7';
  if (['cat','dog','bird','horse','sheep','cow','bear'].includes(label)) return '#3fb950';
  return '#d29922';
}

function createCameraTile(camera) {
  const tile = h('div', { class: 'camera-tile', 'data-camera-id': camera.camera_id });

  const modeBadgeClass = camera.mode === 'local' ? 'badge-local' :
                         camera.mode === 'server' ? 'badge-server' :
                         camera.mode === 'starting' ? 'badge-starting' : 'badge-offline';
  const modeText = camera.mode === 'local' ? 'Local' :
                   camera.mode === 'server' ? 'Server' :
                   camera.mode === 'starting' ? 'Starting…' : 'Offline';

  const header = h('div', { class: 'camera-tile-header' },
    h('span', { class: 'camera-name' }, camera.name || camera.camera_id),
    h('div', { class: 'camera-meta' },
      h('span', { class: `badge ${modeBadgeClass}` }, modeText),
      h('span', {}, camera.fps != null ? camera.fps.toFixed(1) + ' fps' : '—')
    )
  );

  const img = h('img', { class: 'camera-preview', alt: '', 'aria-hidden': 'true' });
  const canvas = h('canvas', { class: 'camera-overlay', 'aria-hidden': 'true' });
  const offline = h('div', { class: 'camera-offline' },
    h('span', { class: 'camera-offline-icon' }, '📷'),
    h('span', {}, camera.connected ? 'Waiting for frames…' : 'Disconnected')
  );

  tile.append(header, img, canvas, offline);
  cameraGrid.appendChild(tile);

  const badgeEl = tile.querySelector('.badge');
  const fpsEl = tile.querySelector('.camera-meta span:last-child');
  const offlineEl = tile.querySelector('.camera-offline');
  const offlineText = offlineEl.querySelector('span:last-child');

  return {
    tile,
    img,
    canvas,
    offlineEl,
    offlineText,
    badgeEl,
    fpsEl,
    lastFrameTime: 0,
    pendingSrc: null,
    decoding: false
  };
}

function updateCameraTileUI(camera, ui) {
  const isOffline = !camera.connected || !camera.lastFrame;
  ui.tile.classList.toggle('offline', isOffline);

  const modeBadgeClass = camera.mode === 'local' ? 'badge-local' :
                         camera.mode === 'server' ? 'badge-server' :
                         camera.mode === 'starting' ? 'badge-starting' : 'badge-offline';
  const modeText = camera.mode === 'local' ? 'Local' :
                   camera.mode === 'server' ? 'Server' :
                   camera.mode === 'starting' ? 'Starting…' : 'Offline';
  ui.badgeEl.className = `badge ${modeBadgeClass}`;
  ui.badgeEl.textContent = modeText;

  ui.fpsEl.textContent = camera.fps != null ? camera.fps.toFixed(1) + ' fps' : '—';

  if (camera.lastFrame) {
    ui.lastFrameTime = Date.now();
    if (!ui.decoding && camera.lastFrame !== ui.pendingSrc) {
      ui.pendingSrc = camera.lastFrame;
      ui.decoding = true;
      ui.img.src = camera.lastFrame;
      ui.img.decode().then(() => {
        ui.decoding = false;
        if (ui.pendingSrc === camera.lastFrame) {
          drawDetections(ui.canvas, camera.lastDetections, camera.frame_w || 640, camera.frame_h || 480, ui.img);
        }
      }).catch(() => {
        ui.decoding = false;
      });
    }
    ui.offlineEl.style.display = 'none';
  } else {
    ui.offlineEl.style.display = 'flex';
    ui.offlineText.textContent = camera.connected ? 'Waiting for frames…' : 'Disconnected';
  }
}

function drawDetections(canvas, detections, frameW, frameH, img) {
  const ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);

  if (!detections || !detections.length) return;

  const imgRect = getImageDisplayRect(img, rect.width, rect.height);
  if (!imgRect) return;

  const scaleX = imgRect.width / frameW;
  const scaleY = imgRect.height / frameH;
  const offsetX = imgRect.x;
  const offsetY = imgRect.y;

  const minScore = settings ? settings.min_score : 0;
  const enabled = settings ? new Set(settings.labels) : null;
  detections.forEach(det => {
    if (det.score < minScore || (enabled && !enabled.has(det.label))) return;
    const [x, y, w, h] = det.bbox;
    const color = getLabelColor(det.label);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(offsetX + x * scaleX, offsetY + y * scaleY, w * scaleX, h * scaleY);

    const label = `${LABEL_DISPLAY[det.label] || det.label} ${Math.round(det.score * 100)}%`;
    ctx.font = '12px system-ui';
    const metrics = ctx.measureText(label);
    const pad = 4;
    const bx = offsetX + x * scaleX;
    const by = offsetY + y * scaleY;
    const ty = by - 20 < 0 ? by : by - 20;
    ctx.fillStyle = color;
    ctx.fillRect(bx, ty, metrics.width + pad * 2, 20);
    ctx.fillStyle = '#fff';
    ctx.fillText(label, bx + pad, ty + 15);
  });
}

function getImageDisplayRect(img, containerW, containerH) {
  const naturalW = img.naturalWidth || img.width;
  const naturalH = img.naturalHeight || img.height;
  if (!naturalW || !naturalH) return null;

  const imgRatio = naturalW / naturalH;
  const containerRatio = containerW / containerH;

  let displayW, displayH, offsetX, offsetY;
  if (imgRatio > containerRatio) {
    displayW = containerW;
    displayH = containerW / imgRatio;
    offsetX = 0;
    offsetY = (containerH - displayH) / 2;
  } else {
    displayH = containerH;
    displayW = containerH * imgRatio;
    offsetX = (containerW - displayW) / 2;
    offsetY = 0;
  }
  return { x: offsetX, y: offsetY, width: displayW, height: displayH };
}

function checkCameraOffline() {
  const now = Date.now();
  cameras.forEach((camera, id) => {
    const ui = camera._ui;
    if (ui && camera.connected && camera.lastFrame && now - ui.lastFrameTime > 10000) {
      camera.connected = false;
      updateCameraTileUI(camera, ui);
    }
  });
}

function renderEventFilters() {
  eventFiltersEl.innerHTML = '';
  const allBtn = h('button', { class: 'chip' + (eventFilters.labels.size === 0 ? ' chip-person' : ''), type: 'button' }, 'All');
  allBtn.addEventListener('click', () => {
    eventFilters.labels.clear();
    renderEventFilters();
    renderEvents();
  });
  eventFiltersEl.appendChild(allBtn);

  ALL_LABELS.forEach(label => {
    const btn = h('button', {
      class: `chip ${LABEL_COLOR_CLASS[label] || 'chip-other'}` + (eventFilters.labels.has(label) ? '' : ''),
      type: 'button',
      style: { opacity: eventFilters.labels.has(label) ? '1' : '0.5' }
    }, LABEL_DISPLAY[label]);
    btn.addEventListener('click', () => {
      if (eventFilters.labels.has(label)) eventFilters.labels.delete(label);
      else eventFilters.labels.add(label);
      renderEventFilters();
      renderEvents();
    });
    eventFiltersEl.appendChild(btn);
  });

  if (cameras.size > 1) {
    const camFilter = h('select', { class: 'select', style: { width: 'auto' } });
    camFilter.appendChild(h('option', { value: '' }, 'All Cameras'));
    cameras.forEach(c => {
      camFilter.appendChild(h('option', { value: c.camera_id, selected: eventFilters.cameraId === c.camera_id }, c.name || c.camera_id));
    });
    camFilter.addEventListener('change', (e) => {
      eventFilters.cameraId = e.target.value || null;
      renderEvents();
    });
    eventFiltersEl.appendChild(camFilter);
  }
}

function eventMatchesFilters(event) {
  if (eventFilters.cameraId && event.camera_id !== eventFilters.cameraId) return false;
  if (eventFilters.labels.size > 0) {
    const hasLabel = event.labels.some(l => eventFilters.labels.has(l));
    if (!hasLabel) return false;
  }
  return true;
}

function renderEventItem(event, highlight = false) {
  const item = h('div', { class: 'event-item' + (highlight ? ' highlight' : ''), 'data-event-id': event.id });

  const header = h('div', { class: 'event-item-header' });
  const thumb = h('img', {
    class: 'event-thumb',
    src: event.thumb_url || '',
    alt: '',
    loading: 'lazy',
    'aria-hidden': 'true'
  });
  if (!event.thumb_url) thumb.style.visibility = 'hidden';

  const info = h('div', { class: 'event-info' });
  const labelsDiv = h('div', { class: 'event-labels' });
  event.labels.forEach(l => labelsDiv.appendChild(createChip(l)));
  info.appendChild(labelsDiv);

  const cameraDiv = h('div', { class: 'event-camera' }, event.camera_name || event.camera_id);
  info.appendChild(cameraDiv);

  const timeDiv = h('div', { class: 'event-time', 'data-ts': event.ts, title: formatAbsoluteTime(event.ts) }, formatRelativeTime(event.ts));
  info.appendChild(timeDiv);

  const score = event.max_score != null ? (event.max_score * 100).toFixed(0) + '%' : '—';
  const scoreDiv = h('div', { class: 'event-score' }, score);
  info.appendChild(scoreDiv);

  header.append(thumb, info);
  item.appendChild(header);

  item.appendChild(h('div', { class: 'event-divider' }));

  const footer = h('div', { class: 'event-footer' });
  footer.appendChild(h('span', {}, `ID: ${event.id}`));
  const viewBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', 'data-action': 'view' }, 'View');
  viewBtn.addEventListener('click', (e) => { e.stopPropagation(); openEventModal(event); });
  footer.appendChild(viewBtn);
  item.appendChild(footer);

  item.addEventListener('click', (e) => {
    if (e.target.tagName !== 'BUTTON') openEventModal(event);
  });

  return item;
}

let highlightNext = false;

function renderEvents() {
  const filtered = events.filter(eventMatchesFilters).slice(0, 500);
  eventsList.innerHTML = '';
  filtered.forEach((event, i) => {
    eventsList.appendChild(renderEventItem(event, i === 0 && highlightNext));
  });
  highlightNext = false;
  loadMoreBtn.disabled = loadingMore || lastLoadCount < 50;
  loadMoreBtn.textContent = loadingMore ? 'Loading…' : 'Load more';
}

function prependEvent(event) {
  if (!eventMatchesFilters(event)) return;
  const item = renderEventItem(event, true);
  eventsList.prepend(item);
  highlightNext = true;
  while (eventsList.children.length > 500) {
    eventsList.lastChild.remove();
  }
  if (documentHidden && notificationEnabled) {
    notifyEvent(event);
  }
}

async function loadEvents(beforeId = null) {
  if (loadingMore) return;
  loadingMore = true;
  loadMoreBtn.disabled = true;
  loadMoreBtn.textContent = 'Loading…';
  try {
    const params = new URLSearchParams({ limit: '50' });
    if (beforeId) params.set('before', beforeId);
    const res = await fetch(`/api/events?${params}`, { credentials: 'same-origin' });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    const data = await res.json();
    const newEvents = data.events || [];
    lastLoadCount = newEvents.length;
    if (beforeId) {
      events.push(...newEvents);
    } else {
      events = newEvents;
    }
    if (events.length > 500) events.splice(500);
    lastEventId = events.length > 0 ? events[events.length - 1].id : null;
    renderEventFilters();
    renderEvents();
  } catch (err) {
    showToast('Failed to load events', 'error');
  } finally {
    loadingMore = false;
    loadMoreBtn.disabled = false;
    loadMoreBtn.textContent = 'Load more';
  }
}

loadMoreBtn.addEventListener('click', () => loadEvents(lastEventId));

async function loadSettings() {
  try {
    const res = await fetch('/api/settings', { credentials: 'same-origin' });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    settings = await res.json();
    renderSettings();
    renderEventFilters();
  } catch (err) {
    showToast('Failed to load settings', 'error');
  }
}

function renderSettings() {
  if (!settings) return;
  settingsBody.innerHTML = '';

  const labelsSection = h('div', { class: 'settings-section' },
    h('h3', { class: 'settings-section-title' }, 'Detection Labels'),
    h('div', { class: 'label-grid', id: 'labelGrid' })
  );
  const labelGrid = labelsSection.querySelector('#labelGrid');
  ALL_LABELS.forEach(label => {
    const enabled = settings.labels?.includes(label) ?? false;
    const wrapper = h('label', { class: 'label-toggle' },
      h('input', { type: 'checkbox', value: label, checked: enabled }),
      h('span', { class: 'label-toggle-text' },
        h('span', { class: 'label-color', style: { background: getLabelColor(label) } }),
        h('span', {}, LABEL_DISPLAY[label] || label)
      )
    );
    labelGrid.appendChild(wrapper);
  });
  settingsBody.appendChild(labelsSection);

  const slidersSection = h('div', { class: 'settings-section' },
    h('h3', { class: 'settings-section-title' }, 'Thresholds & Timing')
  );
  const sliderConfigs = [
    { id: 'minScore', label: 'Min Score', min: 0.1, max: 0.95, step: 0.01, value: settings.min_score ?? 0.55, fmt: v => parseFloat(v).toFixed(2) },
    { id: 'cooldown', label: 'Cooldown (seconds)', min: 0, max: 3600, step: 1, value: settings.cooldown_s ?? 30, fmt: v => v },
    { id: 'previewFps', label: 'Preview FPS', min: 0.2, max: 10, step: 0.1, value: settings.preview_fps ?? 2, fmt: v => parseFloat(v).toFixed(1) },
    { id: 'detectFps', label: 'Detect FPS', min: 0.2, max: 10, step: 0.1, value: settings.detect_fps ?? 2, fmt: v => parseFloat(v).toFixed(1) }
  ];
  sliderConfigs.forEach(cfg => {
    const field = h('div', { class: 'field' },
      h('label', { class: 'label', for: cfg.id }, cfg.label + ': ', h('span', { id: cfg.id + 'Val' }, cfg.fmt(cfg.value))),
      h('input', { type: 'range', class: 'slider', id: cfg.id, min: cfg.min, max: cfg.max, step: cfg.step, value: cfg.value, oninput: e => {
        document.getElementById(cfg.id + 'Val').textContent = cfg.fmt(e.target.value);
      }})
    );
    slidersSection.appendChild(field);
  });
  settingsBody.appendChild(slidersSection);

  const modeSection = h('div', { class: 'settings-section' },
    h('h3', { class: 'settings-section-title' }, 'Detection Mode'),
    h('div', { class: 'grid grid-1', id: 'modeGrid' })
  );
  const modeGrid = modeSection.querySelector('#modeGrid');
  MODE_OPTIONS.forEach(opt => {
    const checked = settings.mode === opt.value;
    const div = h('label', { class: 'mode-option' },
      h('input', { type: 'radio', name: 'mode', value: opt.value, checked }),
      h('div', { class: 'mode-option-title' }, opt.title),
      h('div', { class: 'mode-option-desc' }, opt.desc)
    );
    modeGrid.appendChild(div);
  });
  settingsBody.appendChild(modeSection);

  const retentionSection = h('div', { class: 'settings-section' },
    h('h3', { class: 'settings-section-title' }, 'Retention'),
    h('div', { class: 'field' },
      h('label', { class: 'label', for: 'retention' }, 'Retention (days): ', h('span', { id: 'retentionVal' }, settings.retention_days ?? 14)),
      h('input', { type: 'range', class: 'slider', id: 'retention', min: 1, max: 365, step: 1, value: settings.retention_days ?? 14, oninput: e => {
        document.getElementById('retentionVal').textContent = e.target.value;
      }})
    )
  );
  settingsBody.appendChild(retentionSection);

  // Security section - Change PIN
  const securitySection = h('div', { class: 'settings-section' },
    h('h3', { class: 'settings-section-title' }, 'Security'),
    h('div', { class: 'pin-change-form', id: 'pinChangeForm' })
  );
  const pinChangeForm = securitySection.querySelector('#pinChangeForm');
  pinChangeForm.append(
    h('p', { class: 'welcome-step-text', style: { marginBottom: '16px' } }, 'Change your dashboard PIN. This will log out all other sessions.'),
    h('form', { id: 'pinForm', novalidate: true },
      h('div', { class: 'field' },
        h('label', { class: 'label', for: 'currentPin' }, 'Current PIN'),
        h('div', { class: 'input-wrapper' },
          h('input', { class: 'input', type: 'password', id: 'currentPin', name: 'current_pin', inputmode: 'numeric', pattern: '[0-9]*', autocomplete: 'current-password', required: true, 'aria-describedby': 'currentPinError' }),
          h('button', { class: 'btn btn-ghost btn-sm toggle-visibility', type: 'button', 'aria-label': 'Show PIN', 'aria-pressed': 'false' }, 'Show')
        ),
        h('div', { id: 'currentPinError', class: 'field-error', 'aria-live': 'polite' })
      ),
      h('div', { class: 'field' },
        h('label', { class: 'label', for: 'newPin' }, 'New PIN'),
        h('div', { class: 'input-wrapper' },
          h('input', { class: 'input', type: 'password', id: 'newPin', name: 'new_pin', inputmode: 'numeric', pattern: '[0-9]*', autocomplete: 'new-password', required: true, 'aria-describedby': 'newPinError newPinHint' }),
          h('button', { class: 'btn btn-ghost btn-sm toggle-visibility', type: 'button', 'aria-label': 'Show PIN', 'aria-pressed': 'false' }, 'Show')
        ),
        h('div', { id: 'newPinError', class: 'field-error', 'aria-live': 'polite' }),
        h('div', { class: 'field-hint', id: 'newPinHint' }, '4–12 digits')
      ),
      h('div', { class: 'field' },
        h('label', { class: 'label', for: 'newPinConfirm' }, 'Confirm New PIN'),
        h('div', { class: 'input-wrapper' },
          h('input', { class: 'input', type: 'password', id: 'newPinConfirm', name: 'new_pin_confirm', inputmode: 'numeric', pattern: '[0-9]*', autocomplete: 'new-password', required: true, 'aria-describedby': 'newPinConfirmError' }),
          h('button', { class: 'btn btn-ghost btn-sm toggle-visibility', type: 'button', 'aria-label': 'Show PIN', 'aria-pressed': 'false' }, 'Show')
        ),
        h('div', { id: 'newPinConfirmError', class: 'field-error', 'aria-live': 'polite' })
      ),
      h('div', { class: 'field' },
        h('button', { class: 'btn btn-primary w-full', type: 'button', id: 'pinChangeBtn' }, 'Change PIN')
      )
    )
  );
  settingsBody.appendChild(securitySection);

  // Attach PIN change form handlers
  const pinForm = document.getElementById('pinForm');
  const currentPinInput = document.getElementById('currentPin');
  const newPinInput = document.getElementById('newPin');
  const newPinConfirmInput = document.getElementById('newPinConfirm');
  const currentPinError = document.getElementById('currentPinError');
  const newPinError = document.getElementById('newPinError');
  const newPinConfirmError = document.getElementById('newPinConfirmError');
  const newPinHint = document.getElementById('newPinHint');
  const pinChangeBtn = document.getElementById('pinChangeBtn');

  function validatePinChange() {
    const current = currentPinInput.value;
    const newPin = newPinInput.value;
    const confirm = newPinConfirmInput.value;
    let valid = true;

    currentPinError.textContent = '';
    newPinError.textContent = '';
    newPinConfirmError.textContent = '';
    currentPinInput.classList.remove('invalid');
    newPinInput.classList.remove('invalid');
    newPinConfirmInput.classList.remove('invalid');

    if (!current) {
      currentPinError.textContent = 'Enter your current PIN';
      currentPinInput.classList.add('invalid');
      valid = false;
    }
    if (!PIN_RE.test(newPin)) {
      newPinError.textContent = 'PIN must be 4–12 digits';
      newPinInput.classList.add('invalid');
      valid = false;
    }
    if (confirm && newPin !== confirm) {
      newPinConfirmError.textContent = 'PINs do not match';
      newPinConfirmInput.classList.add('invalid');
      valid = false;
    }
    pinChangeBtn.disabled = !valid;
    return valid;
  }

  newPinInput.addEventListener('input', () => {
    validatePinChange();
    newPinHint.textContent = PIN_RE.test(newPinInput.value) ? '✓ Valid' : '4–12 digits';
    newPinHint.style.color = PIN_RE.test(newPinInput.value) ? 'var(--ok)' : 'var(--muted)';
  });
  newPinConfirmInput.addEventListener('input', validatePinChange);
  currentPinInput.addEventListener('input', validatePinChange);

  pinForm.addEventListener('submit', (e) => e.preventDefault());

  pinChangeBtn.addEventListener('click', async () => {
    if (!validatePinChange()) return;

    const currentPin = currentPinInput.value;
    const newPin = newPinInput.value;
    const newPinConfirm = newPinConfirmInput.value;

    if (newPin !== newPinConfirm) {
      newPinConfirmError.textContent = 'PINs do not match';
      newPinConfirmInput.classList.add('invalid');
      return;
    }

    pinChangeBtn.disabled = true;
    pinChangeBtn.textContent = 'Changing…';

    try {
      const res = await fetch('/api/pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ current_pin: currentPin, new_pin: newPin, new_pin_confirm: newPinConfirm })
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        showToast('PIN changed. Other sessions logged out.', 'success');
        pinForm.reset();
        newPinHint.textContent = '4–12 digits';
        newPinHint.style.color = 'var(--muted)';
      } else if (res.status === 401) {
        currentPinError.textContent = 'Current PIN is incorrect';
        currentPinInput.classList.add('invalid');
        showToast('Current PIN is incorrect', 'error');
      } else if (res.status === 422) {
        showToast(data.detail || 'Invalid input', 'error');
      } else {
        showToast(data.detail || 'Failed to change PIN', 'error');
      }
    } catch (err) {
      showToast('Network error. Please check your connection.', 'error');
    } finally {
      pinChangeBtn.disabled = false;
      pinChangeBtn.textContent = 'Change PIN';
    }
  });

  // Toggle visibility buttons
  document.querySelectorAll('#pinChangeForm .toggle-visibility').forEach(btn => {
    btn.addEventListener('click', () => {
      const wrapper = btn.closest('.input-wrapper');
      const input = wrapper.querySelector('input');
      const isPassword = input.type === 'password';
      input.type = isPassword ? 'text' : 'password';
      btn.setAttribute('aria-pressed', isPassword ? 'true' : 'false');
      btn.textContent = isPassword ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', isPassword ? 'Hide PIN' : 'Show PIN');
    });
  });
}

async function saveSettings() {
  const labels = Array.from(settingsBody.querySelectorAll('#labelGrid input:checked')).map(i => i.value);
  const min_score = parseFloat(settingsBody.querySelector('#minScore').value);
  const cooldown_s = parseInt(settingsBody.querySelector('#cooldown').value, 10);
  const preview_fps = parseFloat(settingsBody.querySelector('#previewFps').value);
  const detect_fps = parseFloat(settingsBody.querySelector('#detectFps').value);
  const mode = settingsBody.querySelector('input[name="mode"]:checked')?.value || 'auto';
  const retention_days = parseInt(settingsBody.querySelector('#retention').value, 10);

  const patch = { labels, min_score, cooldown_s, preview_fps, detect_fps, mode, retention_days };

  try {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(patch)
    });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    if (res.status === 422) {
      const data = await res.json();
      showToast(data.detail || 'Invalid settings', 'error');
      return;
    }
    settings = await res.json();
    showToast('Settings saved', 'success');
    closeDrawer(settingsDrawer);
  } catch (err) {
    showToast('Failed to save settings', 'error');
  }
}

settingsSave.addEventListener('click', saveSettings);

function openDrawer(drawer) {
  drawer.classList.remove('hidden');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  drawerOverlay.classList.add('visible');
  drawerOverlay.classList.remove('hidden');
  drawerOverlay.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  const closeBtn = drawer.querySelector('.modal-close, .add-camera-close');
  if (closeBtn) closeBtn.focus();
}

function closeDrawer(drawer) {
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  drawerOverlay.classList.remove('visible');
  setTimeout(() => {
    drawerOverlay.classList.add('hidden');
    drawerOverlay.setAttribute('aria-hidden', 'true');
    if (drawer.classList.contains('add-camera-panel')) {
      drawer.classList.add('hidden');
    }
    document.body.style.overflow = '';
  }, 200);
}

settingsBtn.addEventListener('click', () => openDrawer(settingsDrawer));
settingsClose.addEventListener('click', () => closeDrawer(settingsDrawer));
drawerOverlay.addEventListener('click', () => {
  if (settingsDrawer.classList.contains('open')) closeDrawer(settingsDrawer);
  if (addCameraPanel.classList.contains('open')) closeDrawer(addCameraPanel);
});

addCameraBtn.addEventListener('click', () => {
  const host = location.host;
  const protocol = location.protocol === 'https:' ? 'https:' : 'http:';
  const url = `${protocol}//${host}/camera`;
  addCameraUrl.textContent = url;
  addCameraSetupLink.href = `/setup?host=${encodeURIComponent(host)}`;
  openDrawer(addCameraPanel);
});
addCameraClose.addEventListener('click', () => closeDrawer(addCameraPanel));

function openEventModal(event) {
  eventModalImage.src = `/api/events/${event.id}/snapshot.jpg`;
  eventModalDetails.innerHTML = '';
  const details = [
    { label: 'Camera', value: event.camera_name || event.camera_id },
    { label: 'Time', value: formatAbsoluteTime(event.ts) },
    { label: 'Labels', value: event.labels.map(l => LABEL_DISPLAY[l] || l).join(', ') },
    { label: 'Max Score', value: event.max_score != null ? (event.max_score * 100).toFixed(1) + '%' : '—' },
    { label: 'Frame', value: `${event.frame_w}×${event.frame_h}` },
    { label: 'Detections', value: String(event.detections.length) }
  ];
  details.forEach(d => {
    eventModalDetails.appendChild(h('div', { class: 'event-modal-detail' },
      h('span', { class: 'event-modal-detail-label' }, d.label),
      h('span', { class: 'event-modal-detail-value' }, d.value)
    ));
  });
  eventModalDelete.dataset.eventId = event.id;
  eventModalOverlay.classList.remove('hidden');
  eventModalOverlay.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  eventModalClose.focus();
}

function closeEventModal() {
  eventModalOverlay.classList.add('hidden');
  eventModalOverlay.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  eventModalImage.src = '';
}

eventModalClose.addEventListener('click', closeEventModal);
eventModalCloseBtn.addEventListener('click', closeEventModal);
eventModalOverlay.addEventListener('click', (e) => { if (e.target === eventModalOverlay) closeEventModal(); });

eventModalDelete.addEventListener('click', async () => {
  const id = eventModalDelete.dataset.eventId;
  if (!confirm('Delete this event?')) return;
  try {
    const res = await fetch(`/api/events/${id}`, { method: 'DELETE', credentials: 'same-origin' });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    if (res.ok) {
      showToast('Event deleted', 'success');
      events = events.filter(e => e.id !== parseInt(id, 10));
      renderEvents();
      closeEventModal();
    } else {
      showToast('Failed to delete', 'error');
    }
  } catch (err) {
    showToast('Network error', 'error');
  }
});

async function loadStats() {
  try {
    const res = await fetch('/api/stats', { credentials: 'same-origin' });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
    const data = await res.json();
    statsBar.innerHTML = '';
    statsBar.append(
      h('div', { class: 'stat-item' }, h('span', {}, 'Events total:'), h('span', { class: 'stat-value' }, String(data.events_total ?? 0))),
      h('div', { class: 'stat-item' }, h('span', {}, 'Last 24h:'), h('span', { class: 'stat-value' }, String(data.events_24h ?? 0))),
      h('div', { class: 'stat-item' }, h('span', {}, 'Disk:'), h('span', { class: 'stat-value' }, (data.disk_bytes / 1024 / 1024).toFixed(1) + ' MB'))
    );
  } catch (err) {
    statsBar.textContent = 'Failed to load stats';
  }
}

function updateRelativeTimes() {
  document.querySelectorAll('.event-time[data-ts]').forEach(el => {
    const ts = parseInt(el.dataset.ts, 10);
    if (!isNaN(ts)) el.textContent = formatRelativeTime(ts);
  });
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/viewer`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    reconnectDelay = 1000;
    setConnectionStatus(true);
  };

  ws.onclose = (e) => {
    setConnectionStatus(false);
    if (e.code === 4401) {
      window.location.href = '/login?next=' + encodeURIComponent(location.pathname);
      return;
    }
    if (e.code === 4403) {
      window.location.href = '/welcome';
      return;
    }
    scheduleReconnect();
  };

  ws.onerror = () => {};

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (err) {
      console.warn('Invalid WS message', err);
    }
  };
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    connectWebSocket();
  }, reconnectDelay);
}

function setConnectionStatus(connected) {
  connectionStatus.classList.toggle('connected', connected);
  connectionStatus.classList.toggle('disconnected', !connected);
  statusText.textContent = connected ? 'Connected' : 'Disconnected';
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case 'cameras':
      msg.cameras.forEach(c => {
        let camera = cameras.get(c.camera_id);
        if (!camera) {
          camera = { ...c, _ui: createCameraTile(c) };
          cameras.set(c.camera_id, camera);
        } else {
          Object.assign(camera, c);
          updateCameraTileUI(camera, camera._ui);
        }
      });
      renderEventFilters();
      break;
    case 'settings':
      settings = msg.settings;
      renderSettings();
      renderEventFilters();
      break;
    case 'frame':
      const cam = cameras.get(msg.camera_id);
      if (cam) {
        cam.lastFrame = `data:image/jpeg;base64,${msg.jpeg}`;
        cam.lastDetections = msg.detections || [];
        cam.frame_w = msg.frame_w;
        cam.frame_h = msg.frame_h;
        cam.connected = true;
        updateCameraTileUI(cam, cam._ui);
      }
      break;
    case 'event':
      events.unshift(msg.event);
      if (events.length > 500) events.pop();
      lastEventId = events.length > 0 ? events[events.length - 1].id : null;
      prependEvent(msg.event);
      break;
  }
}

function sendPing() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}
setInterval(sendPing, 20000);

async function requestNotificationPermission() {
  if (!('Notification' in window)) return;
  try {
    notificationPermission = await Notification.requestPermission();
    notificationEnabled = notificationPermission === 'granted';
    updateNotifButton();
    savePrefs();
  } catch (err) {
    notificationPermission = 'denied';
    notificationEnabled = false;
    updateNotifButton();
  }
}

function updateNotifButton() {
  notifBtn.setAttribute('aria-pressed', notificationEnabled ? 'true' : 'false');
  notifBtn.title = notificationEnabled ? 'Disable notifications' : 'Enable notifications';
  notifBtn.innerHTML = notificationEnabled
    ? '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>'
    : '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
}

notifBtn.addEventListener('click', () => {
  if (notificationEnabled) {
    notificationEnabled = false;
    updateNotifButton();
    savePrefs();
    showToast('Notifications disabled', 'info');
  } else {
    requestNotificationPermission();
  }
});

function notifyEvent(event) {
  const now = Date.now();
  const key = event.camera_id;
  const last = lastNotifTime.get(key) || 0;
  if (now - last < 10000) return;
  lastNotifTime.set(key, now);

  const primaryLabel = event.labels[0] || 'motion';
  const title = `SecCam: ${LABEL_DISPLAY[primaryLabel] || primaryLabel}`;
  const body = `${event.camera_name || event.camera_id} · ${event.max_score != null ? (event.max_score * 100).toFixed(0) : '?'}%`;

  try {
    new Notification(title, {
      body,
      tag: `seccam-${event.camera_id}`
    });
  } catch (err) {}

  if (beepEnabled) playBeep();

  unreadCount++;
  updateTitle();
}

async function playBeep() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = 880;
    gain.gain.value = 0.1;
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.1);
  } catch (err) {}
}

function updateTitle() {
  if (documentHidden) {
    document.title = unreadCount > 0 ? `(${unreadCount}) SecCam` : 'SecCam';
  } else {
    document.title = 'SecCam';
    unreadCount = 0;
  }
}

document.addEventListener('visibilitychange', () => {
  documentHidden = document.hidden;
  updateTitle();
})

function loadPrefs() {
  try {
    const stored = localStorage.getItem('seccam_prefs');
    if (stored) {
      const prefs = JSON.parse(stored);
      notificationEnabled = prefs.notifications === true;
      beepEnabled = prefs.beep === true;
      updateNotifButton();
    }
  } catch (err) {}
}

function savePrefs() {
  try {
    localStorage.setItem('seccam_prefs', JSON.stringify({
      notifications: notificationEnabled,
      beep: beepEnabled
    }));
  } catch (err) {}
}

logoutForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
    window.location.href = '/login';
  } catch (err) {
    window.location.href = '/login';
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (settingsDrawer.classList.contains('open')) closeDrawer(settingsDrawer);
    if (addCameraPanel.classList.contains('open')) closeDrawer(addCameraPanel);
    if (!eventModalOverlay.classList.contains('hidden')) closeEventModal();
  }
});

async function init() {
  loadPrefs();
  await loadSettings();
  await loadEvents();
  await loadStats();
  connectWebSocket();
  relativeTimeTimer = setInterval(updateRelativeTimes, 30000);
  offlineCheckTimer = setInterval(checkCameraOffline, 2000);
}

init();
