export class ApiError extends Error {
  constructor(status, detail, code, retryAfter) {
    super(detail || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.retryAfter = retryAfter ?? null;
  }
}

export function csrfToken() {
  if (typeof document === 'undefined') return null;
  for (const part of document.cookie.split(';')) {
    const i = part.indexOf('=');
    if (i > 0 && part.slice(0, i).trim() === 'seccam_csrf') {
      try { return decodeURIComponent(part.slice(i + 1).trim()); } catch { return null; }
    }
  }
  return null;
}

export async function api(path, { method = 'GET', body, signal, redirectOn401 = true } = {}) {
  const headers = {};
  const init = { method, headers, signal, credentials: 'same-origin' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (method !== 'GET') {
    const token = csrfToken();
    if (token) headers['X-CSRF-Token'] = token;
  }
  const res = await fetch(path, init);
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    const ra = parseInt(res.headers.get('Retry-After') || '', 10);
    const err = new ApiError(res.status, data && data.detail, data && data.code, Number.isFinite(ra) ? ra : null);
    if (res.status === 401 && redirectOn401) {
      location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search));
    }
    throw err;
  }
  return data;
}

export function wsUrl(path) {
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
}

export function safeNext(next) {
  if (typeof next !== 'string' || !next.startsWith('/')) return '/';
  if (next.startsWith('//') || next.startsWith('/\\')) return '/';
  if (/[\u0000-\u001f\u007f]/.test(next)) return '/';
  return next;
}
