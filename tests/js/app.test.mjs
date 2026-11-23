import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { decodeViewerFrame } from '../../static/js/frame.js';
import {
  MAX_BUFFERED, shouldSkipFrame, captureConstraints, pickDevice, sendIntervalMs, closeReason,
  detectionsChanged, labelCategory,
} from '../../static/js/camera-logic.js';

const shapes = JSON.parse(readFileSync(new URL('../contract_shapes.json', import.meta.url)));

function build({ magic = 'SC', version = 2, type = 1, header = shapes.ViewerFrameHeader, jpeg = [0xff, 0xd8, 1, 2, 3], hlen } = {}) {
  const hb = new TextEncoder().encode(typeof header === 'string' ? header : JSON.stringify(header));
  const out = new Uint8Array(8 + hb.length + jpeg.length);
  out[0] = magic.charCodeAt(0); out[1] = magic.charCodeAt(1); out[2] = version; out[3] = type;
  new DataView(out.buffer).setUint32(4, hlen ?? hb.length, false);
  out.set(hb, 8);
  out.set(jpeg, 8 + hb.length);
  return out;
}

test('decodeViewerFrame round trip (ArrayBuffer and Uint8Array)', () => {
  const bytes = build();
  for (const input of [bytes.buffer, bytes]) {
    const { header, jpeg } = decodeViewerFrame(input);
    assert.deepEqual(header, shapes.ViewerFrameHeader);
    assert.deepEqual([...jpeg], [0xff, 0xd8, 1, 2, 3]);
  }
});

test('decodeViewerFrame handles non-ASCII headers and an empty jpeg', () => {
  const header = { ...shapes.ViewerFrameHeader, camera_id: 'cam-é' };
  const { header: h, jpeg } = decodeViewerFrame(build({ header, jpeg: [] }));
  assert.equal(h.camera_id, 'cam-é');
  assert.equal(jpeg.length, 0);
});

test('decodeViewerFrame rejects bad magic, version, type and lengths', () => {
  assert.throws(() => decodeViewerFrame(build({ magic: 'XX' })), /magic/);
  assert.throws(() => decodeViewerFrame(build({ version: 1 })), /version/);
  assert.throws(() => decodeViewerFrame(build({ version: 3 })), /version/);
  assert.throws(() => decodeViewerFrame(build({ type: 2 })), /type/);
  assert.throws(() => decodeViewerFrame(build({ hlen: 100000 })), /overrun/);
  assert.throws(() => decodeViewerFrame(build({ hlen: 0xffffffff })), /overrun/);
  assert.throws(() => decodeViewerFrame(new Uint8Array([0x53, 0x43, 2, 1, 0, 0])), /short/);
  assert.throws(() => decodeViewerFrame(new ArrayBuffer(0)));
  assert.throws(() => decodeViewerFrame(build({ header: 'not json' })), /header/);
  assert.throws(() => decodeViewerFrame(build({ header: '[1]' })), /header/);
});

test('shouldSkipFrame', () => {
  assert.equal(MAX_BUFFERED, 524288);
  assert.equal(shouldSkipFrame(0, false), false);
  assert.equal(shouldSkipFrame(524288, false), false);
  assert.equal(shouldSkipFrame(524289, false), true);
  assert.equal(shouldSkipFrame(0, true), true);
});

test('captureConstraints', () => {
  const s = { capture_width: 960 };
  assert.deepEqual(captureConstraints(s), { video: { width: { ideal: 960 }, facingMode: 'environment' }, audio: false });
  assert.deepEqual(captureConstraints(s, 'abc'), { video: { width: { ideal: 960 }, deviceId: { exact: 'abc' } }, audio: false });
});

test('pickDevice', () => {
  const d = (deviceId, label, kind = 'videoinput') => ({ deviceId, kind, label });
  const list = [d('mic', 'Mic', 'audioinput'), d('front', 'Front Camera'), d('back', 'Back Camera 0'), d('other', 'Cam')];
  assert.equal(pickDevice(list, 'other'), 'other');
  assert.equal(pickDevice(list, 'gone'), 'back');
  assert.equal(pickDevice(list, null), 'back');
  assert.equal(pickDevice([d('mic', 'Rear mic', 'audioinput'), d('a', 'USB'), d('b', 'HD')], null), 'a');
  assert.equal(pickDevice([d('x', 'Environment facing')], null), 'x');
  assert.equal(pickDevice([d('mic', 'Mic', 'audioinput')], 'mic'), null);
  assert.equal(pickDevice([], null), null);
  assert.equal(pickDevice([d('a', '')], 'a'), 'a');
});

test('sendIntervalMs', () => {
  const s = { preview_fps: 2, detect_fps: 5 };
  assert.equal(sendIntervalMs(s, 'local'), 500);
  assert.equal(sendIntervalMs(s, 'starting'), 500);
  assert.equal(sendIntervalMs(s, 'server'), 200);
  assert.equal(sendIntervalMs({ preview_fps: 4, detect_fps: 1 }, 'server'), 250);
});

test('closeReason', () => {
  assert.equal(closeReason(4401), 'revoked');
  assert.equal(closeReason(4409), 'replaced');
  assert.equal(closeReason(4403), 'setup');
  assert.equal(closeReason(4429), 'rate_limited');
  for (const c of [1000, 1001, 1006, 1011, 4400, 0]) assert.equal(closeReason(c), 'retry');
});

test('detectionsChanged', () => {
  const a = [{ label: 'person', score: 0.9, bbox: [100, 100, 50, 50] }];
  assert.equal(detectionsChanged(null, a, 640, 480), true);
  assert.equal(detectionsChanged(a, [{ ...a[0], bbox: [101, 100, 50, 50] }], 640, 480), false);
  assert.equal(detectionsChanged(a, [{ ...a[0], bbox: [140, 100, 50, 50] }], 640, 480), true);
  assert.equal(detectionsChanged(a, [{ ...a[0], bbox: [100, 130, 50, 50] }], 640, 480), true);
  assert.equal(detectionsChanged(a, [], 640, 480), true);
  assert.equal(detectionsChanged(a, [{ label: 'dog', score: 0.9, bbox: [100, 100, 50, 50] }], 640, 480), true);
});

test('labelCategory', () => {
  assert.equal(labelCategory('person'), 'person');
  assert.equal(labelCategory('bus'), 'vehicle');
  assert.equal(labelCategory('dog'), 'animal');
  assert.equal(labelCategory('umbrella'), 'object');
  assert.equal(labelCategory('constructor'), 'object');
});

test('camera-logic and frame import without DOM globals', () => {
  assert.equal(typeof document, 'undefined');
});

test('canFallbackToServer only for auto', async () => {
  const { canFallbackToServer } = await import('../../static/js/camera-logic.js');
  assert.equal(canFallbackToServer('auto'), true);
  assert.equal(canFallbackToServer('local'), false);
  assert.equal(canFallbackToServer('server'), false);
});

test('inferenceTooSlow skips warm-up samples', async () => {
  const { inferenceTooSlow } = await import('../../static/js/camera-logic.js');
  assert.equal(inferenceTooSlow([5000, 5000, 100, 100, 100, 100]), null);
  assert.equal(inferenceTooSlow([5000, 5000, 100, 100, 100, 100, 100]), false);
  assert.equal(inferenceTooSlow([10, 10, 1300, 1300, 1300, 1300, 1300]), true);
  assert.equal(inferenceTooSlow([]), null);
});
