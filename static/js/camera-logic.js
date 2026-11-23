// Pure decision logic for the camera page. No DOM or browser globals at import time.
export const MAX_BUFFERED = 524288;

export const ALL_LABELS = [
  'person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle',
  'cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear',
  'backpack', 'handbag', 'suitcase', 'umbrella',
];

const CATEGORIES = {
  person: 'person',
  car: 'vehicle', truck: 'vehicle', bus: 'vehicle', motorcycle: 'vehicle', bicycle: 'vehicle',
  cat: 'animal', dog: 'animal', bird: 'animal', horse: 'animal', sheep: 'animal', cow: 'animal', bear: 'animal',
};

// person | vehicle | animal | object (matches the --label-* theme variables)
export function labelCategory(label) {
  return Object.prototype.hasOwnProperty.call(CATEGORIES, label) ? CATEGORIES[label] : 'object';
}

export function shouldSkipFrame(bufferedAmount, encodeInFlight) {
  return bufferedAmount > MAX_BUFFERED || Boolean(encodeInFlight);
}

export function captureConstraints(settings, deviceId = null) {
  return {
    video: {
      width: { ideal: settings.capture_width },
      ...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: 'environment' }),
    },
    audio: false,
  };
}

export function pickDevice(devices, storedId) {
  const cams = (devices || []).filter((d) => d && d.kind === 'videoinput');
  if (!cams.length) return null;
  if (storedId && cams.some((d) => d.deviceId === storedId)) return storedId;
  const back = cams.find((d) => /back|rear|environment/i.test(d.label || ''));
  return (back || cams[0]).deviceId;
}

export function sendIntervalMs(settings, effectiveMode) {
  return 1000 / (effectiveMode === 'server'
    ? Math.max(settings.preview_fps, settings.detect_fps)
    : settings.preview_fps);
}

export function closeReason(code) {
  if (code === 4401) return 'revoked';
  if (code === 4409) return 'replaced';
  if (code === 4403) return 'setup';
  if (code === 4429) return 'rate_limited';
  return 'retry';
}

// Local mode: should this detection set be sent to the server? True when it differs from the last
// sent set (count, label set, or any box moved/resized by more than 5% of the frame).
export function detectionsChanged(prev, next, frameW, frameH) {
  if (!prev) return true;
  if (next.length !== prev.length) return true;
  const a = new Set(prev.map((d) => d.label));
  const b = new Set(next.map((d) => d.label));
  if (a.size !== b.size || [...a].some((l) => !b.has(l))) return true;
  for (const nd of next) {
    const od = prev.find((d) => d.label === nd.label);
    if (!od) return true;
    for (let i = 0; i < 4; i++) {
      const span = i % 2 === 0 ? frameW : frameH;
      if (Math.abs(nd.bbox[i] - od.bbox[i]) > 0.05 * span) return true;
    }
  }
  return false;
}

// Only auto mode may fall back to server detection; an explicit "local" must stay local.
export function canFallbackToServer(mode) {
  return mode === 'auto';
}

// Auto-mode speed check over raw inference times (ms). The first `warmup` samples (shader compile) are
// ignored. Returns null until `need` further samples exist, then true when their mean exceeds limitMs.
export function inferenceTooSlow(samples, { warmup = 2, need = 5, limitMs = 1200 } = {}) {
  const used = samples.slice(warmup, warmup + need);
  if (used.length < need) return null;
  return used.reduce((a, b) => a + b, 0) / used.length > limitMs;
}
