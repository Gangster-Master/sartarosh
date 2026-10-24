// Telegram.WebApp glue: version-gated calls, MainButton, BackButton, haptics, popups.
export const tg = window.Telegram && window.Telegram.WebApp;

/** Run fn only when the client supports minVersion; never throws. */
export function call(minVersion, fn) {
  if (!tg) return undefined;
  try {
    if (minVersion && !tg.isVersionAtLeast(minVersion)) return undefined;
    return fn();
  } catch (e) {
    return undefined;
  }
}

export const supports = (v) => !!tg && (() => { try { return tg.isVersionAtLeast(v); } catch (e) { return false; } })();

export function hasInitData() {
  return !!(tg && tg.initData && tg.initDataUnsafe && tg.initDataUnsafe.user);
}

export const initData = () => (tg && tg.initData) || '';
export const startParam = () => (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';

function applyColors() {
  call('6.1', () => tg.setHeaderColor('secondary_bg_color'));
  const c = tg && tg.themeParams && tg.themeParams.secondary_bg_color;
  if (c) call('6.1', () => tg.setBackgroundColor(c));
  document.documentElement.dataset.scheme = (tg && tg.colorScheme) || 'light';
}

export function initTg() {
  if (!tg) return;
  call(null, () => tg.ready());
  call(null, () => tg.expand());
  call('7.7', () => tg.disableVerticalSwipes());
  applyColors();
  call(null, () => tg.onEvent('themeChanged', applyColors));
  call(null, () => tg.onEvent('viewportChanged', () => {
    const a = document.activeElement;
    if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) {
      setTimeout(() => a.scrollIntoView({ block: 'center', behavior: 'smooth' }), 50);
    }
  }));
  call(null, () => tg.BackButton.onClick(() => backHandler && backHandler()));
}

// ---------------------------------------------------------------- MainButton
let mbHandler = null;
let mbOpts = null;

/** setMainButton({text, onClick, enabled=true, progress=false}) or setMainButton(null) to hide. */
export function setMainButton(opts) {
  if (!tg) return;
  const mb = tg.MainButton;
  if (mbHandler) { call(null, () => mb.offClick(mbHandler)); mbHandler = null; }
  mbOpts = opts;
  if (!opts) {
    call(null, () => { mb.hideProgress(); mb.hide(); });
    return;
  }
  const enabled = opts.enabled !== false && !opts.progress;
  const tp = tg.themeParams || {};
  call(null, () => mb.setParams({
    text: opts.text,
    is_visible: true,
    is_active: enabled,
    color: enabled ? (tp.button_color || '#2481cc') : (tp.hint_color || '#a0a0a0'),
    text_color: tp.button_text_color || '#ffffff',
  }));
  call(null, () => (opts.progress ? mb.showProgress(false) : mb.hideProgress()));
  if (opts.onClick) {
    const own = () => {
      if (mbOpts !== opts || opts.enabled === false || opts.progress) return;
      opts.onClick();
    };
    mbHandler = own;
    call(null, () => mb.onClick(own));
  }
}

/** Update the current MainButton in place (keeps the handler). */
export function patchMainButton(patch) {
  if (!mbOpts) return;
  setMainButton(Object.assign({}, mbOpts, patch));
}

// ---------------------------------------------------------------- BackButton
let backHandler = null;
export function setBack(handler) {
  backHandler = handler;
  call(null, () => (handler ? tg.BackButton.show() : tg.BackButton.hide()));
}

// ---------------------------------------------------------------- Haptics
export const haptic = {
  select: () => call('6.1', () => tg.HapticFeedback.selectionChanged()),
  impact: (s = 'light') => call('6.1', () => tg.HapticFeedback.impactOccurred(s)),
  notify: (t) => call('6.1', () => tg.HapticFeedback.notificationOccurred(t)),
};

// ---------------------------------------------------------------- Misc
export function closingConfirmation(on) {
  call('6.2', () => (on ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation()));
}

export function openLink(url) {
  if (!url) return;
  if (call(null, () => { tg.openLink(url); return true; })) return;
  window.open(url, '_blank', 'noopener');
}

export function openTgLink(url) {
  if (call('6.1', () => { tg.openTelegramLink(url); return true; })) return;
  openLink(url);
}

export function close() { call(null, () => tg.close()); }

/** Native confirm when available; resolves to null when the native popup cannot be used. */
export function nativeConfirm(text) {
  return new Promise((resolve) => {
    const ok = call('6.2', () => { tg.showConfirm(text, (r) => resolve(!!r)); return true; });
    if (!ok) resolve(null);
  });
}

export function nativeAlert(text) {
  return new Promise((resolve) => {
    const ok = call('6.2', () => { tg.showAlert(text, () => resolve(true)); return true; });
    if (!ok) resolve(null);
  });
}

/** requestContact: resolves {ok, response} ; ok=null when unsupported. */
export function requestContact() {
  return new Promise((resolve) => {
    const ok = call('6.9', () => {
      tg.requestContact((shared, res) => {
        resolve({ ok: !!shared, response: res && res.response ? res.response : null });
      });
      return true;
    });
    if (!ok) resolve({ ok: null, response: null });
  });
}

/** requestWriteAccess (6.9+): resolves true/false; null when unsupported. */
export function requestWriteAccess() {
  return new Promise((resolve) => {
    const ok = call('6.9', () => { tg.requestWriteAccess((allowed) => resolve(!!allowed)); return true; });
    if (!ok) resolve(null);
  });
}
