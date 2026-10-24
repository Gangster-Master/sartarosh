// My bookings: "Kelgusi" / "O'tgan" segments.
import { h, clear, fill, skeleton, errorBox, empty, bookingCard } from '../ui.js';
import { get } from '../api.js';
import { setMainButton, haptic } from '../tg.js';
import { go } from '../router.js';

let lastTab = 'up';

export function render(root) {
  let alive = true;

  async function load() {
    fill(root, h('h1', { class: 'page-title', text: 'Navbatlarim' }), skeleton(3));
    let d;
    try { d = await get('/api/bookings'); } catch (e) {
      if (alive && !e.handled) fill(root, h('h1', { class: 'page-title', text: 'Navbatlarim' }), errorBox(e, load));
      return;
    }
    if (!alive) return;
    const up = d.upcoming || [];
    const past = d.past || [];
    if (!up.length && !past.length) {
      fill(root, h('h1', { class: 'page-title', text: 'Navbatlarim' }),
        empty('🗓', "Hali navbat yo'q", "Birinchi navbatingizni bir daqiqada oling."));
      setMainButton({ text: 'Yozilish', onClick: () => go('book') });
      return;
    }
    if (!up.length) lastTab = 'past';
    const list = h('div', { class: 'stack' });
    const segs = h('div', { class: 'seg', role: 'tablist' });
    const tabs = [['up', `Kelgusi${up.length ? ' · ' + up.length : ''}`], ['past', "O'tgan"]];
    function draw() {
      segs.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.k === lastTab));
      const items = lastTab === 'up' ? up : past;
      clear(list);
      if (!items.length) {
        list.appendChild(empty(lastTab === 'up' ? '🗓' : '🕘', lastTab === 'up' ? "Kelgusi navbat yo'q" : "O'tgan navbatlar yo'q", null,
          lastTab === 'up' && { text: 'Yozilish', onClick: () => go('book') }));
        return;
      }
      items.forEach((b) => list.appendChild(bookingCard(b, () => go(`booking/${b.id}`))));
    }
    for (const [k, label] of tabs) {
      segs.appendChild(h('button', { type: 'button', role: 'tab', 'data-k': k, text: label,
        onclick: () => { if (lastTab !== k) { haptic.select(); lastTab = k; draw(); } } }));
    }
    fill(root, h('h1', { class: 'page-title', text: 'Navbatlarim' }), segs, list);
    draw();
    setMainButton({ text: 'Yangi navbat', onClick: () => go('book') });
  }

  load();
  return () => { alive = false; };
}
