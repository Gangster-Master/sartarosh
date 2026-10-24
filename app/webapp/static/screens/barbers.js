// Barbers list with photos, bio and price table.
import { h, fill, skeleton, errorBox, empty, avatar, fmtPrice, fmtDur } from '../ui.js';
import { get } from '../api.js';
import { setMainButton } from '../tg.js';
import { go } from '../router.js';

export function render(root) {
  let alive = true;
  async function load() {
    fill(root, h('h1', { class: 'page-title', text: 'Ustalar va narxlar' }), skeleton(3));
    let d;
    try { d = await get('/api/barbers'); } catch (e) {
      if (alive && !e.handled) fill(root, h('h1', { class: 'page-title', text: 'Ustalar va narxlar' }), errorBox(e, load));
      return;
    }
    if (!alive) return;
    const list = d.barbers || [];
    fill(root, h('h1', { class: 'page-title', text: 'Ustalar va narxlar' }),
      list.length ? null : empty('💈', "Hozircha ustalar yo'q", null),
      list.map((b) => h('article', { class: 'card barber' },
        h('div', { class: 'barber-head' },
          avatar(b.name, b.photo_url, 64),
          h('div', { class: 'row-main' }, h('div', { class: 'barber-name', text: b.name }), b.bio && h('div', { class: 'hint pre', text: b.bio }))),
        (b.services || []).length ? h('div', { class: 'prices' }, b.services.map((s) => h('div', { class: 'price-row' },
          h('span', { text: s.name }), h('span', { class: 'hint small', text: fmtDur(s.duration_min) }), h('b', { text: fmtPrice(s.price_uzs) })))) : null,
        h('button', { class: 'btn secondary block', type: 'button', text: 'Shu ustaga yozilish', onclick: () => go(`book?barber=${b.id}`) }))));
    setMainButton({ text: 'Yozilish', onClick: () => go('book') });
  }
  load();
  return () => { alive = false; };
}
