// Hash router with an in-memory stack, plus shared session state.
import { setMainButton, setBack, closingConfirmation, haptic, close as tgClose } from './tg.js';
import { h, clear, skeleton, errorBox } from './ui.js';
import { get } from './api.js';

/** Shared session: filled by boot and refreshed after contact / consent. */
export const session = { user: null, botUsername: '', consentText: '', home: null, homePromise: null, shop: null };

export async function refreshMe() {
  const d = await get('/api/me');
  session.user = d.user;
  session.botUsername = d.bot_username || session.botUsername;
  session.consentText = d.consent_text || '';
  session.allowsWrite = d.allows_write_to_bot; // true | false | null (unknown)
  return d.user;
}

export const botLink = () => (session.botUsername ? `https://t.me/${session.botUsername}` : 'https://t.me/');

const SCREENS = {
  home: () => import('./screens/home.js'),
  book: () => import('./screens/book.js'),
  bookings: () => import('./screens/bookings.js'),
  booking: () => import('./screens/booking.js'),
  bonus: () => import('./screens/bonus.js'),
  barbers: () => import('./screens/barbers.js'),
  barber: () => import('./screens/barber.js'),
  consent: () => import('./screens/consent.js'),
};

let root = null;
let stack = [];
let cleanup = null;
let token = 0;
let interceptor = null;
let fatal = false;

export function initRouter(el) { root = el; }

function parse(path) {
  const p = String(path || '').replace(/^#?\/?/, '');
  const [pathname, q = ''] = p.split('?');
  const parts = pathname.split('/').filter(Boolean);
  const query = {};
  for (const kv of q.split('&')) {
    if (!kv) continue;
    const [k, v = ''] = kv.split('=');
    try { query[decodeURIComponent(k)] = decodeURIComponent(v); } catch (e) { /* malformed %-escape: skip */ }
  }
  return { name: parts[0] || 'home', param: parts[1], query };
}

/** Screens call this to handle Back themselves (wizard steps). Return true when handled. */
export function setBackInterceptor(fn) { interceptor = fn; }

function writeHash(path) {
  try { history.replaceState(null, '', '#/' + path); } catch (e) { /* ignore */ }
}

async function show(path) {
  if (!root || fatal) return;
  const my = ++token;
  if (cleanup) { try { cleanup(); } catch (e) { /* ignore */ } cleanup = null; }
  interceptor = null;
  setMainButton(null);
  closingConfirmation(false);
  writeHash(path);
  const r = parse(path);
  const loader = SCREENS[r.name] || SCREENS.home;
  setBack(r.name === 'home' || r.name === 'consent' ? null : back);
  let mod;
  try {
    mod = await loader();
  } catch (e) {
    if (my !== token) return;
    clear(root).appendChild(h('div', { class: 'screen' }, errorBox({ message: "Sahifani yuklab bo'lmadi" }, () => show(path))));
    return;
  }
  if (my !== token) return;
  const screen = h('div', { class: 'screen enter' });
  clear(root).appendChild(screen);
  window.scrollTo(0, 0);
  const c = mod.render(screen, { param: r.param, query: r.query, name: r.name });
  cleanup = typeof c === 'function' ? c : null;
}

export function go(path) {
  haptic.impact('light');
  stack.push(path);
  show(path);
}

/** Replace the whole stack, e.g. reset('home') or reset('home', 'booking/5'). */
export function reset(...paths) {
  stack = paths.length ? paths : ['home'];
  show(stack[stack.length - 1]);
}

/** Replace the top entry without growing the stack. */
export function replace(path) {
  if (stack.length) stack[stack.length - 1] = path; else stack.push(path);
  show(path);
}

export function back() {
  if (interceptor && interceptor() === true) { haptic.impact('light'); return; }
  haptic.impact('light');
  if (stack.length > 1) { stack.pop(); show(stack[stack.length - 1]); }
  else reset('home');
}

export function currentPath() { return stack[stack.length - 1] || 'home'; }

export function startAt(path) {
  const r = parse(path);
  stack = r.name === 'home' || r.name === 'consent' ? [path] : ['home', path];
  show(path);
}

/** Full-screen terminal state (no init data, expired session, blocked). */
export function showFatal(kind, text) {
  fatal = true;
  token++;
  if (cleanup) { try { cleanup(); } catch (e) { /* ignore */ } cleanup = null; }
  setMainButton(null);
  setBack(null);
  closingConfirmation(false);
  const box = document.getElementById('app');
  const V = {
    noinit: ['📱', 'Iltimos, botdagi tugma orqali oching',
      "Ilovani bot ichidagi «🌐 Ilovani ochish» tugmasi yoki menyu tugmasi orqali oching.", null],
    unauthorized: ['⏳', 'Sessiya eskirdi, ilovani qayta oching',
      "Xavfsizlik uchun sessiya muddati cheklangan. Ilovani yopib, bot orqali qayta oching.", { text: 'Yopish', onClick: tgClose }],
    blocked: ['🚫', text || 'Hisobingiz bloklangan.', "Savollar bo'lsa, sartaroshxona bilan bog'laning.", null],
  }[kind] || ['⚠️', text || 'Xatolik yuz berdi', '', { text: 'Qayta yuklash', onClick: () => location.reload() }];
  const action = V[3];
  clear(box).appendChild(h('div', { class: 'fatal' },
    h('div', { class: 'fatal-ico', text: V[0] }),
    h('h1', { text: V[1] }),
    V[2] && h('p', { class: 'hint', text: V[2] }),
    action && h('button', { class: 'btn primary', onclick: action.onClick, text: action.text })));
}

export function loading(screen) { screen.appendChild(skeleton(3)); }
