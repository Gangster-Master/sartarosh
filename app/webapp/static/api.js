// fetch wrapper: tma auth header, JSON, timeouts, error normalisation, global error codes.
import { initData } from './tg.js';

export class ApiError extends Error {
  constructor(status, code, message, ctx, handled = false) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.ctx = ctx || {};
    this.handled = handled; // already shown to the user globally (full screen / toast)
  }
}

const NETWORK_MSG = 'Internet aloqasini tekshiring';
// 'throttled' (429) is not global: the screen shows its own error box with a retry.
const GLOBAL = new Set(['unauthorized', 'user_blocked', 'consent_required']);
let onGlobal = () => {};

/** Register the handler for unauthorized / user_blocked / consent_required. */
export function setGlobalErrorHandler(fn) { onGlobal = fn; }

/**
 * api(method, path, body?, {signal, headers}) -> parsed JSON data, or throws ApiError.
 * A caller-aborted request throws ApiError code 'aborted' (ignore it).
 */
export async function api(method, path, body, opts = {}) {
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, 15000);
  const ext = opts.signal;
  const onExt = () => ctl.abort();
  if (ext) {
    if (ext.aborted) ctl.abort();
    else ext.addEventListener('abort', onExt, { once: true });
  }
  const headers = Object.assign({ Authorization: `tma ${initData()}` }, opts.headers || {});
  const init = { method, headers, signal: ctl.signal, credentials: 'omit', cache: 'no-store' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(rel(path), init);
  } catch (e) {
    if (ext && ext.aborted && !timedOut) throw new ApiError(0, 'aborted', '');
    throw new ApiError(0, 'network', NETWORK_MSG);
  } finally {
    clearTimeout(timer);
    if (ext) ext.removeEventListener('abort', onExt);
  }
  noteServerTime(res);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.ok) return data || {};
  const err = (data && data.error) || {};
  const code = err.code || (res.status === 401 ? 'unauthorized' : res.status >= 500 ? 'error' : 'http_' + res.status);
  const msg = err.message || (res.status >= 500 ? "Xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring." : NETWORK_MSG);
  const ctx = Object.assign({}, err.ctx || {}, err.field ? { field: err.field } : {}, err.reason ? { reason: err.reason } : {});
  const apiErr = new ApiError(res.status, code, msg, ctx);
  if (GLOBAL.has(code)) {
    apiErr.handled = true;
    try { onGlobal(apiErr); } catch (e) { /* ignore */ }
  }
  throw apiErr;
}

// Server clock offset from the HTTP Date header (1 s resolution), so time-gated buttons
// do not depend on a wrong phone clock.
let clockOffset = 0;
function noteServerTime(res) {
  try {
    const t = Date.parse(res.headers.get('Date') || '');
    if (!Number.isNaN(t)) clockOffset = t + 500 - Date.now();
  } catch (e) { /* ignore */ }
}
export const serverNow = () => Date.now() + clockOffset;

/** Relative to the page URL, so the app also works behind a path prefix (e.g. https://host/app/). */
export const rel = (path) => String(path).replace(/^\//, '');

export const get = (path, opts) => api('GET', path, undefined, opts);
export const post = (path, body, opts) => api('POST', path, body, opts);

/** Query string from an object, skipping undefined; null becomes empty ("any"). */
export function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined) continue;
    parts.push(encodeURIComponent(k) + '=' + (v === null ? '' : encodeURIComponent(v)));
  }
  return parts.length ? '?' + parts.join('&') : '';
}

export function uuid() {
  try { if (crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* insecure ctx */ }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const x = [...b].map((n) => n.toString(16).padStart(2, '0')).join('');
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}
