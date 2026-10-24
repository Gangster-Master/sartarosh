// Booking wizard: service -> barber -> date/slot -> comment -> phone -> name -> confirm -> result.
import { h, clear, fill, skeleton, errorBox, empty, row, avatar, fmtPrice, fmtDur, toast, alertMsg, bookingCard } from '../ui.js';
import { get, post, qs, uuid } from '../api.js';
import { setMainButton, haptic, closingConfirmation, openLink, requestWriteAccess, nativeConfirm } from '../tg.js';
import { session, setBackInterceptor, back as routerBack, reset } from '../router.js';
import { phoneGate } from './phone.js';

const ORDER = ['service', 'barber', 'datetime', 'comment', 'phone', 'name', 'confirm'];
const GROUPS = [['morning', 'Ertalab'], ['day', 'Kunduzi'], ['evening', 'Kechqurun']];
const QUOTE_TTL = 120000;
const NO_WRITE = "Bot sizga xabar yubora olmaydi: tasdiq va eslatmalar kelmaydi. Baribir yozilasizmi?";

/**
 * A user who opened the app by link (t.me/<bot>/app) may never have started the bot, so
 * confirmations/reminders would fail. Ask once; Telegram answers at once if already allowed.
 * Resolves false only when the user refused AND then chose not to book.
 */
async function ensureWriteAccess() {
  if (session.allowsWrite === true) return true;
  const r = await requestWriteAccess();
  if (r === null) return true; // old client: it was opened from the bot chat (no direct links < 6.9)
  session.allowsWrite = r;
  if (r) return true;
  const go = await nativeConfirm(NO_WRITE);
  return go === null ? true : go;
}

export function render(root, { query }) {
  const st = {
    service_id: undefined, service: null,
    barber_id: undefined, barber: null, any: null, barber_fixed: false,
    day: null, start_at: null, local: null, comment: '', use_bonus: false,
    idemKey: null, idemSig: '',
  };
  let presetService = query.service ? Number(query.service) : null;
  if (query.barber && Number(query.barber)) { st.barber_id = Number(query.barber); st.barber_fixed = true; }

  const cache = { services: null, barbers: {}, allBarbers: null };
  let steps = [];
  let current = null;
  let stepCleanup = null;
  let alive = true;
  let seq = 0;

  const bar = h('div', { class: 'progress' }, h('i'));
  const body = h('div', { class: 'wiz' });
  root.append(bar, body);

  setBackInterceptor(() => {
    if (current === 'done') { reset('home'); return true; }
    if (steps.length <= 1) return false;
    steps.pop();
    const prev = steps.pop();
    enter(prev, -1);
    return true;
  });

  // ------------------------------------------------------------ step engine
  function leaveStep() {
    if (stepCleanup) { try { stepCleanup(); } catch (e) { /* ignore */ } stepCleanup = null; }
    setMainButton(null);
  }

  async function enter(name, dir) {
    if (!alive) return;
    const my = ++seq;
    leaveStep();
    current = name;
    bar.firstChild.style.width = `${Math.round(((ORDER.indexOf(name) + 1) / ORDER.length) * 100)}%`;
    clear(body).appendChild(skeleton(3));
    window.scrollTo(0, 0);
    let shown;
    try {
      shown = await STEPS[name](dir);
    } catch (e) {
      if (my !== seq || !alive) return;
      if (e && e.code === 'aborted') return;
      if (!e.handled) clear(body).appendChild(errorBox(e, () => { steps = steps.filter((s) => s !== name); enter(name, dir); }));
      steps.push(name);
      return;
    }
    if (my !== seq || !alive) return;
    if (shown) { steps.push(name); return; }
    if (dir > 0) { enter(ORDER[ORDER.indexOf(name) + 1], 1); return; }
    const prev = steps.pop();
    if (prev) enter(prev, -1);
    else { setBackInterceptor(null); routerBack(); }
  }

  /** Go (back) to a step that is in the history, or forward to it if it is not. */
  function jumpTo(name) {
    if (steps.includes(name)) { while (steps.length && steps[steps.length - 1] !== name) steps.pop(); steps.pop(); }
    enter(name, 1);
  }

  const next = (from) => enter(ORDER[ORDER.indexOf(from) + 1], 1);
  const title = (t, sub) => h('div', { class: 'step-head' }, h('h2', { text: t }), sub && h('p', { class: 'hint', text: sub }));
  const show = (...kids) => fill(body, ...kids.filter(Boolean));

  function summaryChips(noBarber) {
    const parts = [];
    if (st.service) parts.push(`✂️ ${st.service.name}`);
    if (noBarber) { /* barber step itself */ } else if (st.barber_id === null) parts.push("🤝 Farqi yo'q");
    else if (st.barber) parts.push(`💈 ${st.barber.name}`);
    if (!parts.length) return null;
    return h('div', { class: 'chips-sum' }, parts.map((p) => h('span', { class: 'chip-sum', text: p })));
  }

  // ------------------------------------------------------------ steps
  const STEPS = {
    async service() {
      if (!cache.services) cache.services = await get('/api/booking/services');
      if (st.barber_fixed && !cache.allBarbers) {
        try { cache.allBarbers = (await get('/api/barbers')).barbers || []; } catch (e) { cache.allBarbers = []; }
      }
      const data = cache.services;
      if (!data.has_services) { st.service_id = null; st.service = null; return false; }
      let list = data.services || [];
      let prices = null;
      const fixed = st.barber_fixed && (cache.allBarbers || []).find((b) => b.id === st.barber_id);
      if (fixed) {
        prices = {};
        for (const s of fixed.services || []) prices[s.service_id] = s;
        const own = list.filter((s) => prices[s.id]);
        if (own.length) list = own;
        if (!st.barber) st.barber = { id: fixed.id, name: fixed.name };
      }
      if (presetService) {
        const s = list.find((x) => x.id === presetService);
        presetService = null;
        if (s) { pickService(s); return false; }
      }
      if (!list.length) {
        show(title('Xizmatni tanlang'), empty('✂️', "Hozircha xizmatlar yo'q", "Keyinroq qayta urinib ko'ring"));
        return true;
      }
      show(
        title('Xizmatni tanlang', fixed ? `Usta: ${fixed.name}` : null),
        h('div', { class: 'card list' }, list.map((s) => {
          const p = prices && prices[s.id];
          return row({
            title: s.name, sub: fmtDur(p ? p.duration_min : s.duration_min),
            right: fmtPrice(p ? p.price_uzs : s.price_uzs),
            cls: st.service_id === s.id ? 'selected' : '',
            onClick: () => { haptic.select(); pickService(s); next('service'); },
          });
        })));
      return true;
    },

    async barber() {
      const key = String(st.service_id == null ? '' : st.service_id);
      if (!cache.barbers[key]) cache.barbers[key] = await get('/api/booking/barbers' + qs({ service_id: st.service_id == null ? null : st.service_id }));
      const data = cache.barbers[key];
      const list = data.barbers || [];
      if (st.barber_fixed) {
        const b = list.find((x) => x.id === st.barber_id);
        if (b) { st.barber = b; return false; }
        toast("Bu usta tanlangan xizmatni ko'rsatmaydi", 'warn');
        st.barber_fixed = false; st.barber_id = undefined; st.barber = null;
      }
      if (!list.length) {
        show(title('Ustani tanlang'), empty('💈', "Hozircha bo'sh usta yo'q", "Boshqa xizmatni tanlab ko'ring"));
        return true;
      }
      if (list.length === 1) { pickBarber(list[0]); return false; }
      const rows = [];
      if (data.any && data.any.available) {
        rows.push(row({
          lead: h('div', { class: 'ava any' }, h('span', { text: '🤝' })),
          title: "Farqi yo'q", sub: "Eng yaqin bo'sh vaqtli usta",
          right: fmtPrice(data.any.price_uzs) + (data.any.price_varies ? ' dan' : ''),
          cls: st.barber_id === null ? 'selected' : '',
          onClick: () => { haptic.select(); pickBarber(null, data.any); next('barber'); },
        }));
      }
      for (const b of list) {
        rows.push(row({
          lead: avatar(b.name, b.photo_url), title: b.name, sub: fmtDur(b.duration_min), right: fmtPrice(b.price_uzs),
          cls: st.barber_id === b.id ? 'selected' : '',
          onClick: () => { haptic.select(); pickBarber(b); next('barber'); },
        }));
      }
      show(title('Ustani tanlang'), summaryChips(true), h('div', { class: 'card list' }, rows));
      return true;
    },

    async datetime() {
      const base = { barber_id: st.barber_id == null ? null : st.barber_id, service_id: st.service_id == null ? null : st.service_id };
      const days = (await get('/api/booking/days' + qs(base))).days || [];
      const strip = h('div', { class: 'strip', role: 'listbox' });
      const slotsBox = h('div', { class: 'slots' });
      const free = days.filter((d) => d.is_working !== false && d.has_free_slots);
      if (!free.length) {
        show(title('Kun va vaqt'), summaryChips(), empty('🗓', "Bo'sh vaqt yo'q", 'Yaqin kunlarda bo\'sh vaqt topilmadi.',
          { text: 'Boshqa usta tanlash', onClick: () => { st.barber_fixed = false; st.barber_id = undefined; st.barber = null; jumpTo('barber'); } }));
        return true;
      }
      if (!st.day || !free.some((d) => d.day === st.day)) { st.day = free[0].day; st.start_at = null; }
      let ctl = null;

      function chip(d) {
        const [wd, num] = String(d.short || '').split(' ');
        const top = d.is_today ? 'Bugun' : d.is_tomorrow ? 'Ertaga' : wd;
        const ok = d.is_working !== false && d.has_free_slots;
        return h('button', {
          class: 'dchip' + (d.day === st.day ? ' on' : ''), type: 'button', disabled: !ok, 'data-day': d.day, title: d.label,
          onclick: () => {
            if (st.day === d.day) return;
            haptic.select(); st.day = d.day; st.start_at = null;
            strip.querySelectorAll('.dchip').forEach((c) => c.classList.toggle('on', c.dataset.day === d.day));
            setMainButton(null); closingConfirmation(false);
            loadSlots();
          },
        }, h('span', { class: 'dchip-top', text: top }), h('span', { class: 'dchip-num', text: num || '' }));
      }
      days.forEach((d) => strip.appendChild(chip(d)));

      async function loadSlots(keep) {
        if (ctl) ctl.abort();
        ctl = new AbortController();
        const my = ctl;
        if (!keep) clear(slotsBox).appendChild(h('div', { class: 'slot-grid' }, Array.from({ length: 8 }, () => h('div', { class: 'sk sk-chip' }))));
        let data;
        try {
          data = await get('/api/booking/slots' + qs(Object.assign({}, base, { day: st.day })), { signal: my.signal });
        } catch (e) {
          if (e.code === 'aborted' || my !== ctl || !alive) return;
          if (!e.handled) clear(slotsBox).appendChild(errorBox(e, () => loadSlots()));
          return;
        }
        if (my !== ctl || !alive) return;
        const slots = data.slots || [];
        if (st.start_at && !slots.some((s) => s.start_at === st.start_at)) {
          st.start_at = null; setMainButton(null); closingConfirmation(false);
          if (keep) toast("Tanlangan vaqt band bo'lib qoldi", 'warn');
        }
        clear(slotsBox);
        if (!slots.length) { slotsBox.appendChild(h('p', { class: 'hint center pad', text: "Bu kunda bo'sh vaqt qolmadi. Boshqa kunni tanlang." })); return; }
        for (const [g, label] of GROUPS) {
          const items = slots.filter((s) => s.group === g);
          if (!items.length) continue;
          slotsBox.append(h('div', { class: 'grp-title', text: label }),
            h('div', { class: 'slot-grid' }, items.map((s) => h('button', {
              class: 'chip' + (s.start_at === st.start_at ? ' on' : ''), type: 'button', 'data-at': s.start_at,
              onclick: (ev) => {
                haptic.select();
                st.start_at = s.start_at; st.slotTime = s.time;
                slotsBox.querySelectorAll('.chip').forEach((c) => c.classList.toggle('on', c === ev.currentTarget));
                closingConfirmation(true);
                setMainButton({ text: `Davom etish · ${s.time}`, onClick: () => next('datetime') });
              },
            }, s.time))));
        }
        if (st.start_at) setMainButton({ text: `Davom etish · ${st.slotTime || ''}`, onClick: () => next('datetime') });
      }

      const onVis = () => { if (document.visibilityState === 'visible') loadSlots(true); };
      document.addEventListener('visibilitychange', onVis);
      stepCleanup = () => { document.removeEventListener('visibilitychange', onVis); if (ctl) ctl.abort(); };

      show(title('Kun va vaqtni tanlang'), summaryChips(), strip, slotsBox);
      const on = strip.querySelector('.dchip.on');
      if (on) on.scrollIntoView({ inline: 'center', block: 'nearest' });
      loadSlots();
      return true;
    },

    async comment() {
      const ta = h('textarea', { class: 'input', rows: 4, maxlength: 300, placeholder: 'Izoh (ixtiyoriy)', value: st.comment });
      const counter = h('div', { class: 'counter', text: `${st.comment.length}/300` });
      ta.addEventListener('input', () => { counter.textContent = `${ta.value.length}/300`; });
      const submit = () => { st.comment = ta.value.trim().slice(0, 300); ta.blur(); next('comment'); };
      show(title('Izoh', "Ustaga biror narsa aytmoqchimisiz? Masalan, soch turmagi haqida."), summaryChips(),
        h('div', { class: 'card pad' }, ta, counter),
        h('button', { class: 'btn secondary block', type: 'button', onclick: submit, text: 'Davom etish' }));
      setMainButton({ text: 'Davom etish', onClick: submit });
      return true;
    },

    async phone() {
      if (session.user && session.user.phone_verified) return false;
      const box = h('div');
      show(box);
      stepCleanup = phoneGate(box, () => next('phone'));
      return true;
    },

    async name() {
      if (!session.user || session.user.name_ok) return false;
      const inp = h('input', { class: 'input', type: 'text', maxlength: 64, autocomplete: 'name', placeholder: 'Ism Familiya', value: session.user.display_name || session.user.first_name || '' });
      const err = h('p', { class: 'err-text' });
      let busy = false;
      const mb = { text: 'Saqlash', onClick: save };
      async function save() {
        if (busy) return;
        busy = true; inp.blur(); err.textContent = '';
        setMainButton(Object.assign({}, mb, { progress: true }));
        try {
          const d = await post('/api/me/name', { name: inp.value.trim() });
          session.user = d.user;
          next('name');
        } catch (e) {
          if (!alive) return;
          busy = false; setMainButton(mb);
          if (!e.handled) { haptic.notify('error'); err.textContent = e.message; }
        }
      }
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
      show(title('Ismingiz', 'Usta sizni qanday chaqirsin?'), h('div', { class: 'card pad' }, inp, err),
        h('button', { class: 'btn secondary block', type: 'button', onclick: save, text: 'Davom etish' }));
      setMainButton(mb);
      return true;
    },

    async confirm() {
      const body0 = { barber_id: st.barber_id == null ? null : st.barber_id, service_id: st.service_id == null ? null : st.service_id, start_at: st.start_at };
      let q = await post('/api/booking/quote', body0);
      let quotedAt = Date.now();
      let submitting = false;
      let requoting = false;
      if (!q.slot_free) { slotGone(); return true; }
      if (!(q.bonus && q.bonus.possible)) st.use_bonus = false;
      const card = h('div');
      const mb = { text: '✅ Tasdiqlash', onClick: submit };

      function draw() {
        const varies = q.price_varies;
        const bon = q.bonus && q.bonus.possible ? q.bonus : null;
        const useB = st.use_bonus && bon;
        const priceEl = useB
          ? h('span', null, h('s', { class: 'hint', text: fmtPrice(q.price_uzs) }), ' ', h('b', { text: bon.final_price_uzs === 0 ? 'Bepul 🎉' : fmtPrice(bon.final_price_uzs) + (varies ? ' dan' : '') }))
          : h('b', { text: fmtPrice(q.price_uzs) + (varies ? ' dan' : '') });
        const lines = [
          ['💈 Usta', q.barber ? q.barber.name : "🤝 Farqi yo'q"],
          q.service && ['✂️ Xizmat', `${q.service.name} · ${fmtDur(q.service.duration_min)}`],
          ['🗓 Sana', q.local ? q.local.label : ''],
          ['🕒 Vaqt', q.local ? `${q.local.time}${q.local.end_time ? '–' + q.local.end_time : ''}` : ''],
          st.comment && ['💬 Izoh', st.comment],
        ].filter(Boolean);
        fill(card, 
          h('div', { class: 'card list' },
            lines.map(([k, v]) => h('div', { class: 'kv' }, h('span', { class: 'hint', text: k }), h('span', { class: 'kv-v', text: v }))),
            h('div', { class: 'kv total' }, h('span', { text: "💰 Narx" }), priceEl)),
          bon && h('label', { class: 'card toggle' },
            h('div', { class: 'row-main' },
              h('div', { class: 'row-title', text: `🎁 Bonusdan foydalanish (−${bon.mode === 'cashback' ? fmtPrice(bon.discount_uzs) : bon.cost})` }),
              h('div', { class: 'row-sub', text: bon.mode === 'cashback' ? `Mavjud: ${fmtPrice(bon.available)}` : `Mavjud shtamplar: ${bon.available}` })),
            h('input', { type: 'checkbox', class: 'switch', checked: !!st.use_bonus, onchange: (e) => { haptic.select(); st.use_bonus = e.target.checked; draw(); } })),
          (q.needs_approval_hint || q.soft_blocked) && h('p', { class: 'note', text: "⏳ Usta tasdiqlashi kerak bo'ladi" }),
          varies && h('p', { class: 'hint small', text: "Narx tanlangan ustaga qarab farq qilishi mumkin." }));
      }

      async function requote() {
        if (submitting || requoting || !alive) return;
        requoting = true;
        setMainButton(Object.assign({}, mb, { progress: true }));
        try {
          q = await post('/api/booking/quote', body0);
          quotedAt = Date.now();
          if (!alive || current !== 'confirm') return;
          if (!q.slot_free) { slotGone(); return; }
          if (!(q.bonus && q.bonus.possible)) st.use_bonus = false;
          draw();
        } catch (e) {
          if (!e.handled && alive) toast(e.message, 'err');
        } finally {
          requoting = false;
          if (alive && current === 'confirm') setMainButton(mb);
        }
      }

      async function submit() {
        if (submitting) return;
        if (Date.now() - quotedAt > QUOTE_TTL) { requote(); return; }
        submitting = true;
        setMainButton(Object.assign({}, mb, { progress: true }));
        if (!(await ensureWriteAccess())) {
          submitting = false;
          if (alive && current === 'confirm') setMainButton(mb);
          return;
        }
        if (!alive) return;
        const payload = Object.assign({}, body0, { comment: st.comment || null, use_bonus: !!st.use_bonus });
        // the key covers everything sent, incl. the bonus toggle: a changed request is a new booking
        const sig = JSON.stringify(payload);
        if (!st.idemKey || st.idemSig !== sig) { st.idemKey = uuid(); st.idemSig = sig; }
        try {
          const res = await post('/api/bookings', payload, { headers: { 'Idempotency-Key': st.idemKey } });
          done(res);
        } catch (e) {
          submitting = false;
          if (!alive) return;
          if (e.code === 'network') {
            const found = await findCreated();
            if (found) { done({ booking: found, auto_confirmed: found.status === 'CONFIRMED' }); return; }
          }
          setMainButton(mb);
          onCreateError(e);
        }
      }

      async function findCreated() {
        try {
          const d = await get('/api/bookings');
          return (d.upcoming || []).find((b) => b.start_at === st.start_at && (st.barber_id == null || (b.barber && b.barber.id === st.barber_id))) || null;
        } catch (e) { return null; }
      }

      function onCreateError(e) {
        if (e.handled) return;
        haptic.notify('error');
        switch (e.code) {
          case 'slot_taken': case 'slot_unavailable': toast(e.message, 'err'); backToSlots(); break;
          case 'phone_required': if (session.user) session.user.phone_verified = false; enter('phone', 1); break;
          case 'invalid_name': if (session.user) session.user.name_ok = false; toast(e.message, 'err'); enter('name', 1); break;
          case 'no_bonus': st.use_bonus = false; toast(e.message, 'warn'); requote(); break;
          case 'client_limit': alertMsg(e.message); break;
          case 'barber_inactive':
            toast(e.message, 'err'); cache.barbers = {};
            st.barber_fixed = false; st.barber_id = undefined; st.barber = null; jumpTo('barber'); break;
          default:
            if (e.ctx && e.ctx.field === 'comment') { toast(e.message, 'err'); jumpTo('comment'); }
            else toast(e.message, 'err');
        }
      }

      const onVis = () => { if (document.visibilityState === 'visible') requote(); };
      document.addEventListener('visibilitychange', onVis);
      const idle = setInterval(() => { if (Date.now() - quotedAt > QUOTE_TTL) requote(); }, 20000);
      stepCleanup = () => { document.removeEventListener('visibilitychange', onVis); clearInterval(idle); };

      draw();
      show(title('Tasdiqlash', "Ma'lumotlarni tekshiring"), card);
      closingConfirmation(true);
      setMainButton(mb);
      return true;
    },
  };

  // ------------------------------------------------------------ helpers
  function pickService(s) {
    if (st.service_id !== s.id) { st.day = null; st.start_at = null; }
    st.service_id = s.id; st.service = s;
  }

  function pickBarber(b, any) {
    const id = b ? b.id : null;
    if (st.barber_id !== id) { st.day = null; st.start_at = null; }
    st.barber_id = id; st.barber = b; st.any = any || null;
  }

  function slotGone() {
    haptic.notify('warning');
    toast("Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", 'warn');
    backToSlots();
  }

  function backToSlots() { st.start_at = null; jumpTo('datetime'); }

  async function done(res) {
    leaveStep();
    current = 'done';
    steps = [];
    closingConfirmation(false);
    bar.firstChild.style.width = '100%';
    haptic.notify('success');
    const b = res.booking;
    const auto = res.auto_confirmed || (b && b.status === 'CONFIRMED');
    const shop = session.shop;
    fill(body, 
      h('div', { class: 'hero center' },
        h('div', { class: 'hero-ico pop', text: auto ? '✅' : '⏳' }),
        h('h2', { text: auto ? 'Navbatingiz tasdiqlandi' : "So'rov yuborildi" }),
        h('p', { class: 'hint', text: auto ? "Sizni kutamiz! Eslatmani botda yuboramiz." : 'Usta tasdiqlagach xabar beramiz.' })),
      b && bookingCard(b, () => reset('home', `booking/${b.id}`)),
      b && h('div', { class: 'card list' },
        h('div', { class: 'kv total' }, h('span', { text: "💰 To'lov" }), h('b', { text: fmtPrice(b.final_price_uzs) })),
        b.bonus_cost > 0 && b.final_price_uzs !== b.price_uzs && h('div', { class: 'kv' }, h('span', { class: 'hint', text: '🎁 Bonus' }), h('span', { text: b.bonus_redeemed ? 'ishlatildi' : 'ishlatiladi' }))),
      shop && (shop.address || shop.map_url) && h('div', { class: 'card list' }, row({
        icon: '📍', title: shop.address || "Xaritada ko'rish", sub: shop.map_url ? 'Xaritada ochish' : null,
        onClick: shop.map_url ? () => openLink(shop.map_url) : null,
      })));
    setMainButton({ text: 'Bosh sahifa', onClick: () => reset('home') });
    session.homePromise = null;
  }

  enter('service', 1);
  return () => {
    alive = false;
    leaveStep();
    closingConfirmation(false);
  };
}
