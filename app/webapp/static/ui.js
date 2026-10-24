// Small DOM helpers. Server data is only ever set via textContent (no innerHTML).
import { nativeConfirm, nativeAlert, haptic } from './tg.js';

/** h('div', {class, onclick, ...attrs}, ...children) */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'disabled' || k === 'checked') el[k] = !!v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const c of kids) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

/** Replace el's children; skips null/false and flattens arrays (unlike Element.append). */
export function fill(el, ...kids) { append(clear(el), kids); return el; }

// ---------------------------------------------------------------- formatting
export function fmtNum(n) { return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }
export function fmtPrice(n) { return `${fmtNum(n)} so'm`; }
export function fmtDur(m) { return `${m} daq`; }
// Public rating, e.g. "⭐4.8 (12)"; `empty` while the barber has no ratings yet.
export function fmtRating(b, empty = '') {
  return b && b.rating_count ? `⭐${Number(b.rating_avg).toFixed(1)} (${b.rating_count})` : empty;
}
export function initials(name) {
  return (name || '?').trim().split(/\s+/).slice(0, 2).map((p) => p[0] || '').join('').toUpperCase() || '?';
}
export function when(local) { return local ? `${local.label}, ${local.time}` : ''; }

// ---------------------------------------------------------------- components
const PILL = { PENDING: 'amber', CONFIRMED: 'green', COMPLETED: 'blue', NO_SHOW: 'red' };
export function pill(b) {
  return h('span', { class: `pill ${PILL[b.status] || ''}`, text: b.status_label || b.status });
}

export function skeleton(cards = 3) {
  const s = h('div', { class: 'skel' });
  for (let i = 0; i < cards; i++) {
    s.appendChild(h('div', { class: 'card' }, h('div', { class: 'sk sk-line' }), h('div', { class: 'sk sk-line w60' })));
  }
  return s;
}

export function empty(icon, title, sub, action) {
  return h('div', { class: 'empty' },
    h('div', { class: 'empty-ico', text: icon }),
    h('div', { class: 'empty-title', text: title }),
    sub && h('div', { class: 'hint', text: sub }),
    action && h('button', { class: 'btn secondary', onclick: action.onClick, text: action.text }));
}

export function errorBox(err, retry) {
  return empty('⚠️', (err && err.message) || 'Xatolik yuz berdi', null,
    retry && { text: "Qayta urinish", onClick: retry });
}

export function avatar(name, url, size = 44) {
  const box = h('div', { class: 'ava', style: `width:${size}px;height:${size}px` }, h('span', { text: initials(name) }));
  if (url) {
    const img = h('img', { src: String(url).replace(/^\//, ''), alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => img.remove());
    box.appendChild(img);
  }
  return box;
}

export function section(title, ...kids) {
  return h('section', { class: 'sec' }, title && h('div', { class: 'sec-title', text: title }), h('div', { class: 'card list' }, ...kids));
}

/** List row: {icon, title, sub, right, onClick, chevron} */
export function row(o) {
  const tag = o.onClick ? 'button' : 'div';
  return h(tag, { class: 'row' + (o.cls ? ' ' + o.cls : ''), onclick: o.onClick, type: o.onClick ? 'button' : null, disabled: o.disabled },
    o.lead || (o.icon && h('span', { class: 'row-ico', text: o.icon })),
    h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: o.title }), o.sub && h('div', { class: 'row-sub', text: o.sub })),
    o.right != null && h('div', { class: 'row-right', text: o.right }),
    o.onClick && o.chevron !== false && h('span', { class: 'chev', 'aria-hidden': 'true' }));
}

/** Booking summary card used by home / bookings / result screens. */
export function bookingCard(b, onClick) {
  const tag = onClick ? 'button' : 'div';
  return h(tag, { class: 'card bk', onclick: onClick, type: onClick ? 'button' : null },
    h('div', { class: 'bk-top' },
      h('div', { class: 'bk-when' }, h('div', { class: 'bk-time', text: b.local ? b.local.time : '' }), h('div', { class: 'bk-date', text: b.local ? b.local.label : '' })),
      pill(b)),
    h('div', { class: 'bk-meta' },
      h('span', { text: `💈 ${b.barber ? b.barber.name : ''}` }),
      b.service && h('span', { text: `✂️ ${b.service.name}` })),
  );
}

// ---------------------------------------------------------------- overlays
function overlayRoot() { return document.getElementById('overlay'); }

let toastTimer = 0;
export function toast(msg, kind) {
  if (!msg) return;
  const root = overlayRoot();
  const old = root.querySelector('.toast');
  if (old) old.remove();
  const t = h('div', { class: 'toast' + (kind ? ' ' + kind : ''), role: 'status', text: msg });
  root.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 250); }, 2800);
}

/** Bottom sheet. Returns {el, close}. onClose fires once. */
export function sheet({ title, body, onClose }) {
  const root = overlayRoot();
  const panel = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true' },
    h('div', { class: 'sheet-grip' }), title && h('div', { class: 'sheet-title', text: title }), body);
  const back = h('div', { class: 'backdrop' }, panel);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    back.classList.remove('show');
    setTimeout(() => back.remove(), 220);
    if (onClose) onClose();
  };
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  root.appendChild(back);
  requestAnimationFrame(() => back.classList.add('show'));
  return { el: panel, close };
}

/** confirm(text, {ok, danger}) -> Promise<boolean>; native popup first, sheet fallback. */
export async function confirm(text, opts = {}) {
  const r = await nativeConfirm(text);
  if (r !== null) return r;
  return new Promise((resolve) => {
    let val = false;
    const s = sheet({
      body: h('div', { class: 'sheet-body' },
        h('p', { class: 'pre', text }),
        h('button', { class: 'btn ' + (opts.danger ? 'danger' : 'primary'), onclick: () => { val = true; s.close(); }, text: opts.ok || 'OK' }),
        h('button', { class: 'btn ghost', onclick: () => s.close(), text: 'Yopish' })),
      onClose: () => resolve(val),
    });
  });
}

export async function alertMsg(text) {
  haptic.notify('warning');
  const r = await nativeAlert(text);
  if (r === null) toast(text, 'warn');
}
