// Home: shop header, upcoming booking, bonus tile, rebook, menu. MainButton "Yozilish".
import { h, clear, fill, skeleton, errorBox, bookingCard, row, section, fmtPrice, toast } from '../ui.js';
import { get } from '../api.js';
import { setMainButton, openLink } from '../tg.js';
import { go, session } from '../router.js';

export function stampsText(progress, required) {
  const n = Math.max(0, Math.min(required || 0, 12));
  let s = '';
  for (let i = 0; i < n; i++) s += i < progress ? '✂️' : '▫️';
  return s;
}

export function render(root) {
  let alive = true;
  setMainButton({ text: 'Yozilish', onClick: () => go('book') });

  async function load(force) {
    let d = null;
    if (!force && session.homePromise) { d = await session.homePromise; session.homePromise = null; }
    if (!d) {
      clear(root).appendChild(skeleton(4));
      try { d = await get('/api/home'); } catch (e) {
        if (alive && !e.handled) clear(root).appendChild(errorBox(e, () => load(true)));
        return;
      }
    }
    if (!alive) return;
    session.home = d;
    session.shop = d.shop;
    if (d.user) session.user = d.user;
    draw(d);
  }

  function draw(d) {
    const shop = d.shop || {};
    // Shop-local "today" comes from the server; weekday 0 = Monday (Python).
    const td = d.today ? new Date(d.today + 'T12:00:00Z') : null;
    const wd = td && !isNaN(td) ? (td.getUTCDay() + 6) % 7 : (new Date().getDay() + 6) % 7;
    const hrs = (shop.hours || []).find((x) => x.weekday === wd);
    const hoursText = hrs && hrs.open ? `Bugun ${hrs.open}–${hrs.close}` : (hrs ? 'Bugun dam olish kuni' : '');

    const head = h('header', { class: 'hero' },
      h('div', { class: 'hero-logo', text: '💈' }),
      h('div', null,
        h('h1', { text: shop.name || 'Sartaroshxona' }),
        hoursText && h('div', { class: 'hint', text: `🕒 ${hoursText}` })));

    const contacts = [];
    if (shop.address || shop.map_url) {
      contacts.push(row({
        icon: '📍', title: shop.address || "Xaritada ko'rish", sub: shop.map_url ? "Xaritada ochish" : null,
        onClick: shop.map_url ? () => openLink(shop.map_url) : null,
      }));
    }
    if (shop.phone) {
      contacts.push(h('a', { class: 'row', href: `tel:${String(shop.phone).replace(/[^\d+]/g, '')}` },
        h('span', { class: 'row-ico', text: '📞' }),
        h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: shop.phone }), h('div', { class: 'row-sub', text: "Qo'ng'iroq qilish" }))));
    }

    let upcoming = null;
    if (d.upcoming) {
      upcoming = h('section', { class: 'sec' },
        h('div', { class: 'sec-title', text: 'Navbatingiz' }),
        bookingCard(d.upcoming, () => go(`booking/${d.upcoming.id}`)),
        d.upcoming_count > 1 && h('button', { class: 'link-btn', onclick: () => go('bookings'), text: `Yana ${d.upcoming_count - 1} ta navbat →` }));
    }

    let bonusTile = null;
    const b = d.bonus;
    if (b && b.enabled) {
      const stamps = b.mode === 'stamps';
      bonusTile = h('button', { class: 'card tile' + (b.can_redeem ? ' ready' : ''), type: 'button', onclick: () => go('bonus') },
        h('div', { class: 'tile-ico', text: '🎁' }),
        h('div', { class: 'row-main' },
          h('div', { class: 'row-title', text: b.can_redeem ? 'Bonus tayyor!' : 'Bonuslarim' }),
          h('div', { class: 'row-sub stamps', text: stamps ? `${stampsText(b.progress, b.required)}  ${b.progress}/${b.required}` : `Balans: ${fmtPrice(b.available != null ? b.available : b.balance)}` })),
        h('span', { class: 'chev' }));
    }

    const rb = d.rebook;
    const rebook = rb && h('button', {
      class: 'btn secondary block', type: 'button',
      onclick: () => go(`book?barber=${rb.barber_id || ''}&service=${rb.service_id || ''}`),
      text: `🔁 Avvalgidek: ${rb.barber_name || ''}${rb.service_name ? ', ' + rb.service_name : ''}`,
    });

    const menu = [
      row({ icon: '📋', title: 'Navbatlarim', onClick: () => go('bookings') }),
      row({ icon: '💈', title: 'Ustalar va narxlar', onClick: () => go('barbers') }),
      row({ icon: '🎁', title: 'Bonuslar', onClick: () => go('bonus') }),
    ];
    if (d.is_barber) menu.push(row({ icon: '✂️', title: 'Usta paneli', sub: 'Bugungi navbatlar', onClick: () => go('barber'), cls: 'accent' }));

    fill(root, 
      head,
      contacts.length ? h('div', { class: 'card list' }, contacts) : null,
      upcoming || h('button', { class: 'card cta', type: 'button', onclick: () => go('book') },
        h('div', { class: 'cta-ico', text: '✂️' }),
        h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: 'Navbatga yozilish' }), h('div', { class: 'row-sub', text: "Qulay vaqtni tanlang — bir daqiqa ichida" })),
        h('span', { class: 'chev' })),
      rebook,
      bonusTile,
      section(null, menu),
    );
  }

  load(false).catch((e) => toast(e.message, 'err'));
  return () => { alive = false; };
}
