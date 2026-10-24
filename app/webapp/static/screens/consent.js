// Consent screen: show the bot's consent text, MainButton "✅ Roziman".
import { h, clear, fill, skeleton, errorBox, toast } from '../ui.js';
import { post } from '../api.js';
import { setMainButton, haptic } from '../tg.js';
import { session, refreshMe, reset } from '../router.js';

export function render(root) {
  let alive = true;

  async function load() {
    clear(root).appendChild(skeleton(2));
    try {
      if (!session.consentText) await refreshMe();
    } catch (e) {
      if (!e.handled && alive) clear(root).appendChild(errorBox(e, load));
      return;
    }
    if (!alive) return;
    if (session.user && !session.user.consent_required) { reset('home'); return; }
    fill(root, 
      h('div', { class: 'hero center' }, h('div', { class: 'hero-ico', text: '📄' }), h('h1', { text: 'Xush kelibsiz!' }),
        h('p', { class: 'hint', text: 'Davom etish uchun shartlarga rozilik bering.' })),
      h('div', { class: 'card pad pre', text: session.consentText || '' }),
    );
    const mb = { text: '✅ Roziman', onClick: accept };
    setMainButton(mb);

    async function accept() {
      setMainButton(Object.assign({}, mb, { progress: true }));
      try {
        const d = await post('/api/me/consent', {});
        session.user = d.user;
        session.consentText = '';
        haptic.notify('success');
        reset('home');
      } catch (e) {
        if (!alive) return;
        setMainButton(mb);
        if (!e.handled) { haptic.notify('error'); toast(e.message, 'err'); }
      }
    }
  }

  load();
  return () => { alive = false; };
}
