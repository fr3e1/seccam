import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { THEMES, DEFAULT_THEME, STORAGE_KEY, isTheme, getStoredTheme } from '../../static/js/theme.js';
import { safeNext, ApiError, csrfToken, wsUrl } from '../../static/js/api.js';
import { backoffDelay } from '../../static/js/backoff.js';
import { fmtBytes, fmtRelative } from '../../static/js/ui.js';

test('THEMES ids, order and count', () => {
  assert.equal(THEMES.length, 8);
  assert.deepEqual(THEMES.map((t) => t.id), ['dracula', 'nord', 'gruvbox-dark', 'tokyo-night',
    'catppuccin-mocha', 'solarized-dark', 'solarized-light', 'one-dark']);
  assert.equal(THEMES.filter((t) => !t.dark).map((t) => t.id).join(), 'solarized-light');
  assert.equal(DEFAULT_THEME, 'tokyo-night');
  assert.equal(STORAGE_KEY, 'seccam.theme');
});

test('isTheme', () => {
  for (const t of THEMES) assert.equal(isTheme(t.id), true);
  for (const bad of ['', 'nope', null, undefined, 5, {}, 'Nord']) assert.equal(isTheme(bad), false);
});

test('getStoredTheme falls back without localStorage', () => {
  assert.equal(getStoredTheme(), DEFAULT_THEME);
});

test('backoffDelay bounds and cap', () => {
  assert.equal(backoffDelay(0, {}, () => 1), 500);
  assert.equal(backoffDelay(0, {}, () => 0), 350);
  assert.equal(backoffDelay(2, {}, () => 1), 2000);
  assert.equal(backoffDelay(30, {}, () => 1), 15000);
  assert.equal(backoffDelay(30, {}, () => 0), Math.round(15000 * 0.7));
  assert.equal(backoffDelay(1, { baseMs: 100, maxMs: 1000, jitter: 0 }, () => 0.5), 200);
  for (let a = 0; a < 40; a++) {
    const d = backoffDelay(a);
    assert.ok(d >= 0 && d <= 15000);
  }
});

test('safeNext', () => {
  assert.equal(safeNext('/camera'), '/camera');
  assert.equal(safeNext('/a?b=c#d'), '/a?b=c#d');
  for (const bad of ['//evil.com', '/\\evil.com', 'http://evil.com', 'evil', '', null, undefined, 5, '/\t/evil.com', '/a\nb']) {
    assert.equal(safeNext(bad), '/');
  }
});

test('ApiError fields', () => {
  const e = new ApiError(429, 'slow down', 'rate_limited', 12);
  assert.ok(e instanceof Error);
  assert.deepEqual([e.status, e.detail, e.code, e.retryAfter], [429, 'slow down', 'rate_limited', 12]);
  assert.equal(new ApiError(500, 'x', 'internal').retryAfter, null);
});

test('api helpers are safe without a DOM', () => {
  assert.equal(csrfToken(), null);
  assert.equal(typeof wsUrl, 'function');
});

test('ui formatters', () => {
  assert.equal(fmtBytes(0), '0 B');
  assert.equal(fmtBytes(1536), '1.5 KB');
  assert.equal(fmtBytes(5 * 1024 * 1024), '5.0 MB');
  assert.equal(fmtRelative(1000, 1000), 'just now');
  assert.equal(fmtRelative(0, 90_000), '1m ago');
  assert.equal(fmtRelative(0, 3 * 3600_000), '3h ago');
});

test('themes.css defines every theme with all variables', () => {
  const css = readFileSync(new URL('../../static/css/themes.css', import.meta.url), 'utf8');
  const vars = ['bg', 'bg-alt', 'bg-hl', 'fg', 'muted', 'border', 'accent', 'accent-fg', 'ok', 'warn', 'err', 'info', 'sel', 'cursor',
    'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white', 'label-person', 'label-vehicle', 'label-animal', 'label-object'];
  for (const t of THEMES) {
    const m = css.match(new RegExp(`:root\\[data-theme="${t.id}"\\][^{]*\\{([^}]*)\\}`));
    assert.ok(m, t.id);
    for (const v of vars) assert.ok(m[1].includes(`--${v}:`), `${t.id} --${v}`);
  }
});

test('pages have no inline script, style or handlers', () => {
  for (const f of ['login', 'welcome', 'pair', 'setup']) {
    const html = readFileSync(new URL(`../../static/${f}.html`, import.meta.url), 'utf8');
    assert.ok(!/<script(?![^>]*\bsrc=)/.test(html), `${f} inline script`);
    assert.ok(!/\sstyle=|\son[a-z]+=/i.test(html), `${f} style/handler attribute`);
    assert.ok(html.indexOf('theme-boot.js') < html.indexOf('themes.css'));
  }
});
