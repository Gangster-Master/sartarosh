// Barber actions shared by the barber panel and the booking detail (barber view).
import { h, toast, confirm, sheet, when } from '../ui.js';
import { post, serverNow } from '../api.js';
import { haptic } from '../tg.js';

const REASONS = [['busy', 'Band'], ['sick', 'Kasal'], [null, 'Sababsiz']];

function pickReason(b) {
  return new Promise((resolve) => {
    let result;
    const ta = h('textarea', { class: 'input', rows: 3, maxlength: 200, placeholder: 'Sababni yozing' });
    const send = h('button', { class: 'btn danger block', type: 'button', text: 'Rad etish',
      onclick: () => { const v = ta.value.trim(); if (!v) { ta.focus(); return; } ta.blur(); result = { reason: v }; s.close(); } });
    const other = h('div', { class: 'stack', hidden: true }, ta, send);
    const rows = REASONS.map(([k, label]) => h('button', { class: 'row', type: 'button',
      onclick: () => { haptic.select(); result = { reason: k }; s.close(); } },
    h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: label }))));
    rows.push(h('button', { class: 'row', type: 'button',
      onclick: () => { haptic.select(); other.hidden = false; ta.focus(); } },
    h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: 'Boshqa…' }))));
    const s = sheet({
      title: `Rad etish sababi`,
      body: h('div', { class: 'sheet-body' },
        h('p', { class: 'hint', text: `${when(b.local)} · ${b.client ? b.client.name : ''}` }),
        h('div', { class: 'card list' }, rows), other,
        h('button', { class: 'btn ghost block', type: 'button', onclick: () => s.close(), text: 'Yopish' })),
      onClose: () => resolve(result),
    });
  });
}

const isoReached = (iso) => !iso || serverNow() >= Date.parse(iso);

/** Buttons for one booking (barber view). onChanged() reloads the caller's data. */
export function barberActions(b, onChanged) {
  const a = b.actions || {};
  const box = h('div', { class: 'acts' });
  let busy = false;

  async function run(kind, path, body, okMsg) {
    if (busy) return;
    busy = true;
    box.querySelectorAll('button').forEach((x) => { x.disabled = true; });
    const btn = box.querySelector(`[data-k="${kind}"]`);
    if (btn) btn.classList.add('loading');
    try {
      const d = await post(`/api/barber/bookings/${b.id}/${path}`, body || {});
      haptic.notify('success');
      const notes = (d.notes || []).filter(Boolean);
      toast(notes.length ? notes.join('\n') : okMsg);
    } catch (e) {
      if (!e.handled) {
        haptic.notify('error');
        toast(e.code === 'invalid_transition' ? "Holat allaqachon o'zgargan" : e.message, 'err');
      }
    }
    busy = false;
    onChanged();
  }

  const btn = (k, text, cls, onclick, disabled, hint) => h('button', { class: `btn small ${cls}`, type: 'button', 'data-k': k, onclick, disabled, title: hint }, text);

  if (a.accept) box.appendChild(btn('accept', '✅ Qabul qilish', 'primary', () => run('accept', 'accept', {}, 'Navbat tasdiqlandi')));
  if (a.reject) {
    box.appendChild(btn('reject', '❌ Rad etish', 'danger-soft', async () => {
      const r = await pickReason(b);
      if (r) run('reject', 'reject', { reason: r.reason }, 'Navbat rad etildi');
    }));
  }
  if (a.done) {
    const ready = isoReached(b.done_from);
    box.appendChild(btn('done', '✅ Bajarildi', 'primary', async () => {
      if (await confirm(`${when(b.local)} · ${b.client ? b.client.name : ''}\nXizmat bajarildimi?`, { ok: '✅ Bajarildi' })) run('done', 'done', {}, 'Bajarildi');
    }, !ready, ready ? null : 'Navbat vaqtidan 30 daqiqa oldin faollashadi'));
  }
  if (a.no_show) {
    const ready = isoReached(b.no_show_from);
    box.appendChild(btn('no_show', '🚫 Kelmadi', 'danger-soft', async () => {
      if (await confirm(`${when(b.local)} · ${b.client ? b.client.name : ''}\nMijoz kelmadimi?`, { ok: '🚫 Kelmadi', danger: true })) run('no_show', 'no-show', {}, 'Kelmadi deb belgilandi');
    }, !ready, ready ? null : 'Navbat boshlanganidan 15 daqiqa keyin faollashadi'));
  }
  if (!box.childNodes.length) return null;
  if ((a.done && !isoReached(b.done_from)) || (a.no_show && !isoReached(b.no_show_from))) {
    const note = h('div', { class: 'hint small full', text: '«Bajarildi» vaqtdan 30 daqiqa oldin, «Kelmadi» boshlanganidan 15 daqiqa keyin faollashadi.' });
    box.appendChild(note);
    // unlock in place when the time comes (the detail screen has no auto-refresh)
    const unlock = () => {
      if (!box.isConnected && unlock.armed) return;
      unlock.armed = true;
      const pending = [];
      for (const [k, iso] of [['done', b.done_from], ['no_show', b.no_show_from]]) {
        const el = box.querySelector(`[data-k="${k}"]`);
        if (!el) continue;
        if (isoReached(iso)) { if (!busy) { el.disabled = false; el.removeAttribute('title'); } } else pending.push(Date.parse(iso));
      }
      if (!pending.length) { note.remove(); return; }
      const wait = Math.min(...pending) - serverNow() + 250;
      setTimeout(unlock, Math.max(1000, Math.min(wait, 60000)));
    };
    setTimeout(unlock, 1000);
  }
  return box;
}

/** Client line for barber cards: name, phone link (only if unmasked), stats. */
export function clientInfo(b) {
  const c = b.client;
  if (!c) return null;
  const phone = c.phone && (c.phone.includes('*')
    ? h('span', { class: 'hint', text: c.phone })
    : h('a', { href: `tel:${c.phone.replace(/[^\d+]/g, '')}`, text: c.phone }));
  const s = c.stats;
  const stats = s && (s.is_new ? '🆕 yangi mijoz' : `${s.visits} tashrif · ${s.no_shows} kelmagan`);
  return h('div', { class: 'client' },
    h('div', { class: 'client-name', text: `👤 ${c.name || 'Mijoz'}` }),
    phone && h('div', null, '📞 ', phone),
    stats && h('div', { class: 'hint small', text: stats }));
}
