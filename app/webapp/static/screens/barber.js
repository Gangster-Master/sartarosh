// Barber panel: day strip + queue with Qabul qilish / Rad etish / Bajarildi / Kelmadi.
import { h, clear, fill, skeleton, errorBox, empty, pill } from '../ui.js';
import { get, qs } from '../api.js';
import { haptic, openTgLink } from '../tg.js';
import { go, botLink } from '../router.js';
import { barberActions, clientInfo } from './barber_actions.js';

let selDay = null;

export function render(root) {
  let alive = true;
  let today = null;
  let days = [];
  let ctl = null;
  let timer = 0;
  const strip = h('div', { class: 'strip' });
  const head = h('div', { class: 'queue-head' });
  const list = h('div', { class: 'stack' });
  const refreshBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Yangilash', text: '↻', onclick: () => { haptic.impact('light'); loadQueue(true); } });

  async function loadDays() {
    fill(root, h('h1', { class: 'page-title', text: 'Usta paneli' }), skeleton(3));
    let d;
    try { d = await get('/api/barber/days'); } catch (e) {
      if (alive && !e.handled) fill(root, h('h1', { class: 'page-title', text: 'Usta paneli' }), errorBox(e, loadDays));
      return;
    }
    if (!alive) return;
    today = d.today;
    days = d.days || [];
    if (!selDay || !days.some((x) => x.day === selDay)) selDay = today;
    drawStrip();
    fill(root, 
      h('div', { class: 'title-row' }, h('h1', { class: 'page-title', text: d.barber ? `✂️ ${d.barber.name}` : 'Usta paneli' }), refreshBtn),
      strip, head, list,
      h('button', { class: 'link-btn center-block', type: 'button', text: 'Boshqa amallar botda →', onclick: () => openTgLink(botLink()) }));
    const on = strip.querySelector('.on');
    if (on) on.scrollIntoView({ inline: 'center', block: 'nearest' });
    loadQueue();
  }

  function drawStrip() {
    clear(strip);
    for (const d of days) {
      const [wd, num] = String(d.short || '').split(' ');
      strip.appendChild(h('button', {
        class: 'dchip' + (d.day === selDay ? ' on' : ''), type: 'button', title: d.label,
        onclick: () => { if (selDay === d.day) return; haptic.select(); selDay = d.day; drawStrip(); loadQueue(); },
      }, h('span', { class: 'dchip-top', text: d.is_today ? 'Bugun' : d.is_tomorrow ? 'Ertaga' : wd }), h('span', { class: 'dchip-num', text: num || '' })));
    }
  }

  async function loadQueue(quiet) {
    if (ctl) ctl.abort();
    ctl = new AbortController();
    const my = ctl;
    if (!quiet) { clear(list).appendChild(skeleton(3)); }
    refreshBtn.classList.add('spin');
    let d;
    try {
      d = await get('/api/barber/queue' + qs({ day: selDay }), { signal: my.signal });
    } catch (e) {
      if (e.code === 'aborted' || my !== ctl || !alive) return;
      refreshBtn.classList.remove('spin');
      if (!e.handled) clear(list).appendChild(errorBox(e, () => loadQueue()));
      return;
    }
    if (my !== ctl || !alive) return;
    refreshBtn.classList.remove('spin');
    if (d.today && today && d.today !== today) { selDay = d.today; loadDays(); return; }
    const items = (d.bookings || []).slice().sort((a, b) => {
      const pa = a.actions && a.actions.accept ? 0 : 1;
      const pb = b.actions && b.actions.accept ? 0 : 1;
      return pa - pb || String(a.start_at).localeCompare(String(b.start_at));
    });
    const label = d.day ? d.day.label : selDay;
    fill(head, h('span', { text: `${label}: ${items.length} ta navbat` }),
      d.pending_count ? h('span', { class: 'pill amber', text: `${d.pending_count} ta kutilmoqda` }) : null);
    clear(list);
    if (!items.length) { list.appendChild(empty('☕️', "Bu kunga navbat yo'q", null)); return; }
    for (const b of items) list.appendChild(card(b));
  }

  function card(b) {
    const pending = b.actions && b.actions.accept;
    return h('article', { class: 'card qcard' + (pending ? ' pending' : '') },
      h('div', { class: 'bk-top' },
        h('button', { class: 'qtime', type: 'button', onclick: () => go(`booking/${b.id}`) },
          h('span', { class: 'bk-time', text: b.local ? b.local.time : '' }),
          h('span', { class: 'hint small', text: b.local && b.local.end_time ? `–${b.local.end_time}` : '' })),
        pill(b)),
      clientInfo(b),
      b.service && h('div', { class: 'small', text: `✂️ ${b.service.name}` }),
      b.comment && h('div', { class: 'comment', text: `💬 ${b.comment}` }),
      barberActions(b, () => alive && loadQueue(true)));
  }

  const onVis = () => { if (document.visibilityState === 'visible' && today) loadQueue(true); };
  document.addEventListener('visibilitychange', onVis);
  timer = setInterval(() => { if (document.visibilityState === 'visible' && today) loadQueue(true); }, 60000);

  loadDays();
  return () => {
    alive = false;
    clearInterval(timer);
    document.removeEventListener('visibilitychange', onVis);
    if (ctl) ctl.abort();
  };
}
