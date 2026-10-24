// Entry module: boot, Telegram glue, global error handling, first route.
import { tg, initTg, hasInitData, startParam } from './tg.js';
import { get, setGlobalErrorHandler } from './api.js';
import { toast } from './ui.js';
import { initRouter, startAt, reset, showFatal, session, refreshMe, currentPath } from './router.js';

function onGlobalError(err) {
  switch (err.code) {
    case 'unauthorized': showFatal('unauthorized'); break;
    case 'user_blocked': showFatal('blocked', err.message); break;
    case 'consent_required':
      // During boot /api/me decides; afterwards jump to the consent screen.
      if (session.user && currentPath() !== 'consent') reset('consent');
      break;
    case 'throttled': toast('Biroz kuting', 'warn'); break;
    default: break;
  }
}

async function boot() {
  if (!tg || !hasInitData()) { showFatal('noinit'); return; }
  initTg();
  setGlobalErrorHandler(onGlobalError);
  initRouter(document.getElementById('app'));

  // /api/me and /api/home in parallel; home may 403 (consent) — swallow, home.js refetches.
  session.homePromise = get('/api/home').catch(() => null);
  let user;
  try {
    user = await refreshMe();
  } catch (e) {
    if (e.handled) return;
    showFatal('error', e.message);
    return;
  }
  if (user.blocked) { showFatal('blocked'); return; }
  if (user.consent_required) { reset('consent'); return; }

  // Only our own "#/route" hashes; Telegram puts "#tgWebAppData=..." in the first URL.
  const hash = (location.hash || '').startsWith('#/') ? location.hash.slice(2) : '';
  if (startParam() === 'barber' && user.roles && user.roles.barber && user.roles.barber_id) startAt('barber');
  else if (hash && !hash.startsWith('consent')) startAt(hash);
  else startAt('home');
}

boot();
