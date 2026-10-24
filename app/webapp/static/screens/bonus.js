// Bonus page: balance (stamps or cashback), history, referral link.
import { h, fill, skeleton, errorBox, empty, fmtPrice, toast } from '../ui.js';
import { get } from '../api.js';
import { setMainButton, openTgLink, haptic } from '../tg.js';
import { go, session } from '../router.js';

function stampsBlock(b) {
  const n = Math.max(1, Math.min(b.required || 1, 15));
  const left = Math.max(0, b.required - b.progress);
  const reward = b.reward_type === 'free'
    ? `${b.required + 1}-chi soch olish bepul 🎉`
    : `${b.required + 1}-chi tashrifda ${b.reward_value}% chegirma 🎉`;
  const dots = Array.from({ length: n }, (_, i) => h('span', { class: 'stamp' + (i < b.progress ? ' on' : ''), text: i < b.progress ? '✂️' : '' }));
  return h('div', { class: 'card pad bonus-card' + (b.can_redeem ? ' ready' : '') },
    h('div', { class: 'bonus-big', text: `${b.progress}/${b.required}` }),
    h('div', { class: 'stamps-row' }, dots),
    h('p', { class: 'center', text: b.can_redeem
      ? "🎉 Bonusingiz tayyor! Navbatga yozilishda «🎁 Bonusdan foydalanish» ni yoqing."
      : `Yana ${left} ta tashrifdan keyin ${reward}` }),
    b.reserved ? h('p', { class: 'hint center small', text: `🔒 ${b.reserved} ta shtamp faol navbat uchun band qilingan.` }) : null);
}

function cashBlock(b) {
  return h('div', { class: 'card pad bonus-card' },
    h('div', { class: 'hint center', text: 'Bonus hisobingiz' }),
    h('div', { class: 'bonus-big', text: fmtPrice(b.available != null ? b.available : b.balance) }),
    h('p', { class: 'hint center small', text: `Har tashrifdan ${b.cashback_percent}% qaytadi, to'lovning ${b.max_redeem_percent}% gacha bonus bilan to'lash mumkin.` }),
    b.reserved ? h('p', { class: 'hint center small', text: `🔒 ${fmtPrice(b.reserved)} faol navbat uchun band qilingan.` }) : null);
}

function histRow(tx) {
  const plus = tx.delta > 0;
  const amount = tx.unit === 'cashback' ? fmtPrice(Math.abs(tx.delta)) : `${Math.abs(tx.delta)} ✂️`;
  return h('div', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title', text: tx.reason_label || tx.reason }),
      h('div', { class: 'row-sub', text: [tx.local ? `${tx.local.label}` : '', tx.comment].filter(Boolean).join(' · ') })),
    h('div', { class: 'row-right ' + (plus ? 'pos' : 'neg'), text: `${plus ? '+' : '−'}${amount}` }));
}

function referralBlock(r) {
  const copy = async () => {
    try { await navigator.clipboard.writeText(r.link); haptic.notify('success'); toast('Havola nusxalandi'); } catch (e) {
      const sel = window.getSelection(); const range = document.createRange();
      range.selectNodeContents(linkEl); sel.removeAllRanges(); sel.addRange(range);
      toast('Havolani belgilab, nusxa oling', 'warn');
    }
  };
  const linkEl = h('div', { class: 'reflink', text: r.link });
  const shop = session.shop && session.shop.name;
  const text = `Men shu yerda soch oldiraman${shop ? ' — ' + shop : ''}. Bot orqali navbatga yozil!`;
  return h('section', { class: 'sec' },
    h('div', { class: 'sec-title', text: "Do'stlarni taklif qiling" }),
    h('div', { class: 'card pad stack' },
      h('p', { text: `Do'stingiz shu havola orqali botga kirib, birinchi tashrifini qilsa, sizga +${r.bonus} bonus beriladi.` }),
      linkEl,
      h('div', { class: 'acts' },
        h('button', { class: 'btn secondary', type: 'button', onclick: copy, text: '📋 Nusxa olish' }),
        h('button', { class: 'btn primary', type: 'button', text: '📤 Ulashish',
          onclick: () => openTgLink(`https://t.me/share/url?url=${encodeURIComponent(r.link)}&text=${encodeURIComponent(text)}`) }))));
}

export function render(root) {
  let alive = true;
  async function load() {
    fill(root, h('h1', { class: 'page-title', text: 'Bonuslarim' }), skeleton(2));
    let d;
    try { d = await get('/api/bonus'); } catch (e) {
      if (alive && !e.handled) fill(root, h('h1', { class: 'page-title', text: 'Bonuslarim' }), errorBox(e, load));
      return;
    }
    if (!alive) return;
    const b = d.bonus || {};
    const recent = b.recent || [];
    fill(root, 
      h('h1', { class: 'page-title', text: 'Bonuslarim' }),
      !b.enabled ? empty('🎁', "Bonus dasturi hozir o'chirilgan", null)
        : b.mode === 'cashback' ? cashBlock(b) : stampsBlock(b),
      b.enabled && recent.length ? h('section', { class: 'sec' }, h('div', { class: 'sec-title', text: 'Oxirgi harakatlar' }), h('div', { class: 'card list' }, recent.map(histRow))) : null,
      d.referral && d.referral.enabled && d.referral.link ? referralBlock(d.referral) : null,
    );
    setMainButton({ text: 'Yozilish', onClick: () => go('book') });
  }
  load();
  return () => { alive = false; };
}
