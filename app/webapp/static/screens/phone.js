// Phone gate used by the booking wizard: requestContact -> POST /api/me/contact, or bot fallback.
import { h, fill, toast } from '../ui.js';
import { post } from '../api.js';
import { setMainButton, requestContact, supports, openTgLink, haptic } from '../tg.js';
import { session, refreshMe, botLink } from '../router.js';

const FALLBACK = "Raqamni botga yuboring: chatdagi «📱 Raqamni yuborish» tugmasi";

/** Renders into root; calls onDone() once the user has a verified phone. Returns cleanup. */
export function phoneGate(root, onDone) {
  let alive = true;
  let pollTimer = 0;
  const status = h('p', { class: 'hint center' });
  const botBtn = h('button', { class: 'btn secondary block', type: 'button', onclick: openBot, text: '🤖 Botni ochish' });
  const checkBtn = h('button', { class: 'btn ghost block', type: 'button', onclick: () => check(true), text: '🔄 Yubordim, tekshirish' });
  const fallback = h('div', { class: 'stack', hidden: true }, h('p', { class: 'note', text: FALLBACK }), botBtn, checkBtn);

  fill(root, 
    h('div', { class: 'hero center' },
      h('div', { class: 'hero-ico', text: '📱' }),
      h('h2', { text: 'Navbat uchun telefon raqamingiz kerak' }),
      h('p', { class: 'hint', text: "Usta siz bilan bog'lana olishi uchun raqamingizni bir marta yuboring." })),
    status, fallback);

  const mb = { text: '📱 Raqamni yuborish', onClick: ask };

  // The bot's «📱 Raqamni yuborish» keyboard only exists inside its booking flow, so ask the
  // server to put it into the chat first; webapp_contact saves the reply [finding #1].
  async function openBot() {
    try { await post('/api/me/request-phone', {}); } catch (e) { /* still open the chat */ }
    if (!alive) return;
    openTgLink(botLink());
  }

  function showFallback() {
    fallback.hidden = false;
    setMainButton(supports('6.9') ? mb : null); // keep "retry" where requestContact exists
  }

  async function check(manual) {
    try {
      const u = await refreshMe();
      if (!alive) return false;
      if (u.phone_verified) { finish(); return true; }
      if (manual) toast('Raqam hali kelmadi', 'warn');
    } catch (e) {
      if (manual && !e.handled) toast(e.message, 'err');
    }
    return false;
  }

  function finish() {
    clearTimeout(pollTimer);
    haptic.notify('success');
    toast('✅ Raqamingiz saqlandi');
    onDone();
  }

  async function ask() {
    setMainButton(Object.assign({}, mb, { progress: true }));
    const r = await requestContact();
    if (!alive) return;
    if (r.ok === null || r.ok === false) { showFallback(); return; }
    if (r.response) {
      try {
        const d = await post('/api/me/contact', { response: r.response });
        if (!alive) return;
        session.user = d.user;
        if (d.user && d.user.phone_verified) { finish(); return; }
      } catch (e) {
        if (!alive) return;
        setMainButton(mb);
        if (!e.handled) { haptic.notify('error'); toast(e.message, 'err'); }
        return;
      }
    }
    // Shared without a signed response (older clients): the bot saves it; poll /api/me.
    status.textContent = 'Raqam tekshirilmoqda…';
    const started = Date.now();
    const poll = async () => {
      if (!alive) return;
      if (await check(false)) return;
      if (Date.now() - started >= 10000) { status.textContent = ''; showFallback(); return; }
      pollTimer = setTimeout(poll, 1500);
    };
    poll();
  }

  if (!supports('6.9')) showFallback();
  else setMainButton(mb);

  return () => { alive = false; clearTimeout(pollTimer); };
}
