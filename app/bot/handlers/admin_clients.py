"""Admin → 👥 Mijozlar: phone search (audited), profile, bonus ± with comment,
block / unblock (cancels active bookings), reset no-shows [H2][#48][#69]."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb, BookingCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_dt_short, fmt_price, fmt_stamps, h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, kb
from app.bot.notifier import Notifier
from app.bot.states import AdminClientFlow
from app.db.enums import LoyaltyMode
from app.db.uow import read_tx, write_tx
from app.services import bonus as bonus_svc
from app.services import users
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_clients")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "cl"


async def start_search(target: Target, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    await state.clear()
    await state.set_state(AdminClientFlow.search)
    await screen.render(target, state, t(lang, "admin.cl.ask_phone"), input_cancel_kb(lang))


async def _reply(target: Target, text: str) -> None:
    if isinstance(target, CallbackQuery):
        await views.alert(target, text)
    else:
        await target.answer(text)


async def show_profile(
    target: Target, sm: SM, user: Any, lang: str, uid: int, *, note: str | None = None
) -> None:
    try:
        async with read_tx(sm) as s:
            p = await users.get_client_profile(s, actor_user_id=user.id, user_id=uid)
    except ServiceError as e:
        await _reply(target, views.err_text(lang, e))
        return
    u, st, bn = p.user, p.stats, p.bonus
    lines = []
    if note:
        lines.extend([note, ""])
    uname = f" · @{h(u.tg_username)}" if u.tg_username else ""
    lines.append(f"👤 <b>{h(u.display_name)}</b>{uname}")
    phone = h(u.phone) if u.phone else "—"
    lines.append(t(lang, "admin.cl.phone", phone=phone, verified="✅" if u.phone_verified else "▫️"))
    lines.append(
        t(
            lang,
            "admin.cl.stats",
            visits=st.total_completed,
            noshow=st.no_show_count,
            late=st.late_cancel_count,
        )
    )
    if bn.enabled:
        if bn.mode == LoyaltyMode.STAMPS:
            lines.append(
                t(
                    lang,
                    "admin.cl.stamps",
                    bar=fmt_stamps(bn.progress, bn.required),
                    balance=bn.balance,
                    reserved=bn.reserved,
                )
            )
        else:
            lines.append(
                t(
                    lang,
                    "admin.cl.cash",
                    amount=fmt_price(bn.balance, lang),
                    reserved=fmt_price(bn.reserved, lang),
                )
            )
    if p.soft_blocked:
        lines.append(t(lang, "admin.cl.soft_blocked"))
    if u.is_blocked:
        lines.append(t(lang, "admin.cl.blocked"))
    rows = []
    if p.recent_bookings:
        lines.extend(["", t(lang, "admin.cl.recent")])
        for b in p.recent_bookings[:8]:
            icon = views.STATUS_ICON.get(b.status, "•")
            lines.append(f"{icon} {fmt_dt_short(b.start_at, lang)} · {h(b.barber_name)}")
            rows.append(
                [
                    btn(
                        f"{icon} {fmt_dt_short(b.start_at, lang)} · {b.barber_name}",
                        BookingCb(a="card", id=b.id),
                    )
                ]
            )
    rows.append([btn(t(lang, "admin.cl.bonus_btn"), acb(S, "bon", u.id))])
    rows.append(
        [
            btn(
                t(lang, "admin.cl.unblock_btn") if u.is_blocked else t(lang, "admin.cl.block_btn"),
                acb(S, "blk", u.id),
            ),
            btn(t(lang, "admin.cl.reset_btn"), acb(S, "rns", u.id)),
        ]
    )
    rows.append([btn(t(lang, "admin.cl.new_search"), acb(S, "search"))])
    await screen.render(target, None, "\n".join(lines), kb(rows))


@router.message(StateFilter(AdminClientFlow.search), F.text)
async def on_search(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    try:
        async with write_tx(sm) as s:  # writes an audit row
            found = await users.find_users_by_phone(
                s, actor_user_id=user.id, phone_query=message.text or ""
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    if not found:
        await message.answer(t(lang, "admin.cl.not_found"))
        return
    await state.clear()
    if len(found) == 1:
        await show_profile(message, sm, user, lang, found[0].id)
        return
    rows = [[btn(f"{u.display_name} · {u.phone or '—'}", acb(S, "pick", u.id))] for u in found]
    rows.append([btn(t(lang, "admin.cl.new_search"), acb(S, "search"))])
    await screen.render(message, None, t(lang, "admin.cl.found", n=len(found)), kb(rows))


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_clients(
    call: CallbackQuery,
    callback_data: AdminCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    a, uid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "search":
            await start_search(call, state, sm, user, lang)
        elif a == "pick":
            await state.clear()
            await show_profile(call, sm, user, lang, uid)
        elif a == "bon":
            await state.clear()
            await state.set_state(AdminClientFlow.bonus_delta)
            await state.update_data(user_id=uid)
            await screen.render(
                call, state, t(lang, "admin.cl.ask_delta"), kb([[back_btn(lang, S, "pick", uid)]])
            )
        elif a == "blk":
            async with read_tx(sm) as s:
                target_user = await users.get_user(s, uid)
            if v not in ("on", "off"):
                key = (
                    "admin.cl.unblock_confirm"
                    if target_user.is_blocked
                    else "admin.cl.block_confirm"
                )
                # the confirm button carries the intended state, so a stale or repeated
                # "yes" can never flip the user back [#69]
                intent = "off" if target_user.is_blocked else "on"
                markup = kb(
                    [
                        [
                            btn(t(lang, "common.yes"), acb(S, "blk", uid, intent)),
                            btn(t(lang, "common.no"), acb(S, "pick", uid)),
                        ]
                    ]
                )
                await screen.render(
                    call, None, t(lang, key, name=h(target_user.display_name)), markup
                )
                return
            want = v == "on"
            if want == target_user.is_blocked:
                await show_profile(call, sm, user, lang, uid)
                return
            async with write_tx(sm) as s:
                cancelled = await users.set_blocked(
                    s, actor_user_id=user.id, user_id=uid, blocked=want
                )
            note = (
                t(lang, "admin.cl.unblocked")
                if target_user.is_blocked
                else t(lang, "admin.cl.blocked_ok", n=len(cancelled))
            )
            await show_profile(call, sm, user, lang, uid, note=note)
            for b in cancelled:
                await notifier.booking_cancelled(b, by_client=False)
        elif a == "rns":
            async with write_tx(sm) as s:
                await users.reset_no_shows(s, actor_user_id=user.id, user_id=uid)
            await show_profile(call, sm, user, lang, uid, note=t(lang, "admin.cl.reset_ok"))
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


MAX_BONUS_DELTA = 1_000_000_000


@router.message(StateFilter(AdminClientFlow.bonus_delta), F.text)
async def on_delta(message: Message, state: FSMContext, lang: str) -> None:
    raw = (message.text or "").replace(" ", "")
    try:
        delta = int(raw)
    except ValueError:
        await message.answer(t(lang, "admin.cl.ask_delta"))
        return
    if delta == 0 or abs(delta) > MAX_BONUS_DELTA:
        await message.answer(t(lang, "admin.cl.ask_delta"))
        return
    await state.update_data(delta=delta)
    await state.set_state(AdminClientFlow.bonus_comment)
    await screen.render(
        message, state, t(lang, "admin.cl.ask_comment", delta=f"{delta:+d}"), input_cancel_kb(lang)
    )


@router.message(StateFilter(AdminClientFlow.bonus_comment), F.text)
async def on_comment(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    data = await state.get_data()
    comment = (message.text or "").strip()[:200]
    try:
        async with write_tx(sm) as s:
            await bonus_svc.adjust_bonus(
                s,
                actor_user_id=user.id,
                user_id=int(data["user_id"]),
                delta=int(data["delta"]),
                comment=comment,
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await show_profile(
        message, sm, user, lang, int(data["user_id"]), note=t(lang, "admin.cl.bonus_ok")
    )


@router.message(StateFilter(AdminClientFlow))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
