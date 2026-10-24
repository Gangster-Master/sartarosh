// Booking detail: full card + actions driven only by booking.actions.
import { h, clear, fill, skeleton, errorBox, pill, fmtPrice, fmtDur, toast, confirm, when } from '../ui.js';
import { get, post } from '../api.js';
import { setMainButton, haptic } from '../tg.js';
import { go, session } from '../router.js';
import { barberActions, clientInfo } from './barber_actions.js';

const FINISHED = new Set(['COMPLETED', 'NO_SHOW', 'REJECTED', 'EXPIRED', 'CANCELLED_BY_CLIENT', 'CANCELLED_BY_BARBER']);

export function render(root, { param }) {
  let alive = true;
  let busy = false;
  const id = Number(param);

  async function load(quiet) {
    if (!quiet) clear(root).appendChild(skeleton(2));
    setMainButton(null);
    let b;
    try { b = (await get(`/api/bookings/${id}`)).booking; } catch (e) {
      if (alive && !e.handled) clear(root).appendChild(errorBox(e, () => load()));
      return;
    }
    if (alive) draw(b);
  }

  async function act(path, body, okMsg) {
    if (busy) return null;
    busy = true;
    root.querySelectorAll('.acts button').forEach((x) => { x.disabled = true; });
    try {
      const d = await post(`/api/bookings/${id}/${path}`, body);
      haptic.notify('success');
      toast(typeof okMsg === 'function' ? okMsg(d) : okMsg);
      session.homePromise = null;
      return d;
    } catch (e) {
      if (!e.handled) { haptic.notify('error'); toast(e.message, 'err'); }
      return null;
    } finally {
      busy = false;
      if (alive) load(true);
    }
  }

  function draw(b) {
    const a = b.actions || {};
    const isBarberView = !!b.client;
    const kv = (k, v, cls) => v != null && v !== '' && h('div', { class: 'kv' + (cls ? ' ' + cls : '') }, h('span', { class: 'hint', text: k }), typeof v === 'string' ? h('span', { class: 'kv-v', text: v }) : v);
    // same rule as the bot's booking card: the bonus is reserved at booking, redeemed on completion
    const withBonus = b.bonus_cost > 0 && b.final_price_uzs != null && b.final_price_uzs !== b.price_uzs;
    const price = withBonus
      ? h('span', null, h('s', { class: 'hint', text: fmtPrice(b.price_uzs) }), ' ', h('b', { text: fmtPrice(b.final_price_uzs) }))
      : h('b', { text: fmtPrice(b.final_price_uzs != null ? b.final_price_uzs : b.price_uzs) });

    const acts = h('div', { class: 'acts col' });
    if (!isBarberView) {
      if (a.answer_proposal) {
        acts.append(
          h('div', { class: 'note', text: `🕒 Usta yangi vaqt taklif qildi: ${when(b.local)}` }),
          h('div', { class: 'acts' },
            h('button', { class: 'btn primary', type: 'button', text: '✅ Roziman', onclick: () => act('proposal', { accept: true }, 'Yangi vaqt qabul qilindi') }),
            h('button', { class: 'btn danger-soft', type: 'button', text: "❌ Yo'q", onclick: () => act('proposal', { accept: false }, 'Taklif rad etildi') })));
      }
      if (a.withdraw) {
        acts.appendChild(h('button', { class: 'btn danger-soft block', type: 'button', text: "↩️ So'rovni qaytarib olish",
          onclick: async () => {
            if (await confirm(`${when(b.local)} uchun so'rovni qaytarib olasizmi?`, { ok: 'Ha, qaytarib olish', danger: true })) act('cancel', { withdraw: true }, "So'rov qaytarib olindi");
          } }));
      }
      if (a.cancel) {
        acts.appendChild(h('button', { class: 'btn danger-soft block', type: 'button', text: 'Bekor qilish',
          onclick: async () => {
            let text = `${when(b.local)} dagi ${b.barber ? b.barber.name : ''} bilan navbatni bekor qilasizmi?`;
            if (a.late_cancel_warning) {
              haptic.notify('warning');
              const hrs = session.shop && session.shop.free_cancel_hours;
              text += `\n\n⚠️ Kech bekor qilish: ${hrs ? hrs + ' soatdan' : 'belgilangan vaqtdan'} kam qoldi`;
            }
            if (await confirm(text, { ok: 'Ha, bekor qilish', danger: true })) {
              act('cancel', { withdraw: false }, (d) => (d.late ? 'Navbat bekor qilindi (kech)' : 'Navbat bekor qilindi'));
            }
          } }));
      }
    } else {
      const ba = barberActions(b, () => alive && load(true));
      if (ba) acts.appendChild(ba);
    }

    fill(root, 
      h('div', { class: 'detail-head' },
        h('div', null, h('div', { class: 'big-time', text: b.local ? b.local.time : '' }), h('div', { class: 'hint', text: b.local ? b.local.label : '' })),
        pill(b)),
      isBarberView && h('div', { class: 'card pad' }, clientInfo(b)),
      h('div', { class: 'card list' },
        kv('💈 Usta', b.barber ? b.barber.name : ''),
        b.service && kv('✂️ Xizmat', `${b.service.name} · ${fmtDur(b.duration_min)}`),
        kv('🕒 Vaqt', b.local ? `${b.local.time}${b.local.end_time ? '–' + b.local.end_time : ''}` : ''),
        b.original_local && kv('↪️ Avvalgi vaqt', when(b.original_local)),
        b.comment && kv('💬 Izoh', b.comment),
        kv('💰 Narx', price, 'total'),
        withBonus && kv('🎁 Bonus', `${b.bonus_redeemed ? 'ishlatildi' : 'ishlatiladi'} (−${b.bonus_cost})`),
        (b.cancel_reason_label || b.cancel_reason) && kv('ℹ️ Sabab', b.cancel_reason_label || b.cancel_reason),
        b.late_cancel && kv('⚠️', 'Kech bekor qilingan')),
      acts.childNodes.length ? acts : null,
    );
    if (!isBarberView && FINISHED.has(b.status)) {
      setMainButton({ text: '🔁 Yana yozilish', onClick: () => go(`book?barber=${b.barber ? b.barber.id : ''}&service=${b.service ? b.service.id : ''}`) });
    }
  }

  if (!id) { root.appendChild(errorBox({ message: 'Navbat topilmadi' })); return () => {}; }
  load();
  return () => { alive = false; };
}
