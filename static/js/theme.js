import { api, ApiError } from './api.js';

export const THEMES = [
  { id: 'dracula', name: 'Dracula', dark: true },
  { id: 'nord', name: 'Nord', dark: true },
  { id: 'gruvbox-dark', name: 'Gruvbox Dark', dark: true },
  { id: 'tokyo-night', name: 'Tokyo Night', dark: true },
  { id: 'catppuccin-mocha', name: 'Catppuccin Mocha', dark: true },
  { id: 'solarized-dark', name: 'Solarized Dark', dark: true },
  { id: 'solarized-light', name: 'Solarized Light', dark: false },
  { id: 'one-dark', name: 'One Dark', dark: true },
];
export const DEFAULT_THEME = 'tokyo-night';
export const STORAGE_KEY = 'seccam.theme';

export function isTheme(id) {
  return typeof id === 'string' && THEMES.some((t) => t.id === id);
}

function storage() {
  try { return typeof localStorage === 'undefined' ? null : localStorage; } catch { return null; }
}

export function applyTheme(id, root = document.documentElement) {
  const theme = isTheme(id) ? id : DEFAULT_THEME;
  root.dataset.theme = theme;
  const s = storage();
  if (s) { try { s.setItem(STORAGE_KEY, theme); } catch { /* quota or blocked */ } }
  if (typeof document !== 'undefined' && typeof getComputedStyle === 'function') {
    const meta = document.querySelector('meta[name="theme-color"]');
    const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
    if (meta && bg) meta.setAttribute('content', bg);
  }
  return theme;
}

export function getStoredTheme() {
  const s = storage();
  let v = null;
  if (s) { try { v = s.getItem(STORAGE_KEY); } catch { v = null; } }
  return isTheme(v) ? v : DEFAULT_THEME;
}

export async function saveTheme(id) {
  const applied = applyTheme(id);
  try {
    await api('/api/settings', { method: 'PUT', body: { theme: applied }, redirectOn401: false });
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) throw e;
  }
  return applied;
}

export function syncThemeFromSettings(settings) {
  const id = settings && settings.theme;
  if (isTheme(id) && id !== document.documentElement.dataset.theme) applyTheme(id);
}
