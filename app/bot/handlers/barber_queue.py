"""Barber queue: today, upcoming days, booking card, done / no-show / staff cancel [#25][#30].

Admins can open any booking card too (from the admin bookings list or from
fallback decision messages); services re-check every permission [D5].
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb, BookingCb, HubCb, QueueDayCb
from app.bot.filters import IsBarber, IsStaff
from app.bot.i18n import fmt_date, fmt_price, fmt_time, h, t
from app.bot.keyboards import barber as bkb
from app.bot.keyboards.common import btn, kb
from app.bot.notifier import Notifier
from app.bot.states import StaffCancelFlow
from app.db.enums import Awaiting, BookingSource, BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, settings, users, validation
from app.services.errors import InputError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="barber_queue")
router.message.filter(F.chat.type == "private")

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message

STAFF_ACTIONS = ("card", "done", "ns", "scx", "dd", "dn")  # dd/dn: nightly digest


# ------------------------------------------------------------------ day list


async def show_day(
    target: Target,
    state: FSMContext | None,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
    *,
    day: date | None,
) -> None:
    day = day or clock.local_today()
    try:
        async with read_tx(sm) as s:
            items = await bookings.list_barber_day(
                s, actor_user_id=user.id, barber_id=roles.barber_id, day=day
            )
    except ServiceError as e:
        if isinstance(target, CallbackQuery):
            await views.alert(target, views.err_text(lang, e))
        else:
            await target.answer(views.err_text(lang, e))
        return
    items = sorted(items, key=lambda b: b.start_at)
    today = clock.local_today()
    label = (
        t(lang, "common.today")
        if day == today
        else (t(lang, "common.tomorrow") if day == today + timedelta(days=1) else "")
    )
    title = f"{label}, {fmt_date(day, lang)}" if label else fmt_date(day, lang)
    lines = [t(lang, "barber.day_title", day=title, n=len(items))]
    if not items:
        lines.append("")
        lines.append(t(lang, "barber.day_empty"))
    for b in items:
        icon = views.STATUS_ICON.get(b.status, "•")
        phone = f" · {h(b.client_phone)}" if b.client_phone else ""
        svc = f" · {h(b.service_name)}" if b.service_name else ""
        lines.append(f"{icon} <b>{fmt_time(b.start_at)}</b> {h(b.client_name)}{phone}{svc}")
    await screen.render(target, state, "\n".join(lines), bkb.queue_kb(items, day, lang))


async def show_day_picker(target: Target, state: FSMContext | None, sm: SM, lang: str) -> None:
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
    today = clock.local_today()
    n = max(7, int(shop.booking_horizon_days))
    days = [today + timedelta(days=i) for i in range(min(n, 21))]
    await screen.render(target, state, t(lang, "barber.pick_day"), bkb.days_kb(days, lang))


@router.callback_query(QueueDayCb.filter(), IsBarber(), ANY)
async def on_queue_day(
    call: CallbackQuery,
    callback_data: QueueDayCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    await state.clear()
    if not callback_data.d:
        await show_day_picker(call, None, sm, lang)
        return
    day = views.parse_day(callback_data.d)
    if day is None:
        await views.stale(call, lang)
        return
    await show_day(call, None, sm, user, roles, lang, day=day)


# ------------------------------------------------------------------ booking card


async def card_view(
    sm: SM, user: Any, roles: Any, lang: str, b: Any, *, note: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    stats = None
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
        if b.client_id:
            stats = await users.get_client_stats(s, b.client_id)
    lines = []
    if note:
        lines.extend([note, ""])
    lines.extend(views.booking_lines(b, lang, staff=True))
    if stats is not None:
        lines.append(
            t(lang, "card.stats", visits=stats.total_completed, noshow=stats.no_show_count)
        )
        if stats.is_new:
            lines.append(t(lang, "card.new_client"))
    if b.bonus_cost > 0:
        lines.append(t(lang, "card.bonus_used", price=fmt_price(b.final_price_uzs, lang)))
    if b.status == BookingStatus.COMPLETED and b.auto_completed:
        lines.append(t(lang, "barber.auto_completed"))
    if b.source == BookingSource.MANUAL and b.visit_confirmed is not None:
        lines.append(
            t(lang, "barber.visit_confirmed" if b.visit_confirmed else "barber.visit_denied")
        )

    now = clock.now_utc()
    rows = []
    if b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER:
        rows.extend(bkb.decision_kb(b.id, lang).inline_keyboard)
    if b.status == BookingStatus.CONFIRMED:
        act = []
        if now >= b.start_at - timedelta(minutes=30):
            act.append(btn(t(lang, "barber.done"), BookingCb(a="done", id=b.id)))
        if now >= b.start_at + timedelta(minutes=15):
            act.append(btn(t(lang, "barber.no_show"), BookingCb(a="ns", id=b.id)))
        if act:
            rows.append(act)
    if b.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        rows.append([btn(t(lang, "barber.staff_cancel"), BookingCb(a="scx", id=b.id))])
    if (
        b.status == BookingStatus.COMPLETED
        and b.auto_completed
        and b.completed_at is not None
        and now <= b.completed_at + timedelta(hours=int(shop.noshow_flip_window_hours))
    ):
        rows.append([btn(t(lang, "barber.flip_no_show"), BookingCb(a="ns", id=b.id))])
    if roles.barber_id and roles.barber_id == b.barber_id:
        day = clock.to_local(b.start_at).date()
        rows.append([btn(t(lang, "common.back"), QueueDayCb(d=day.strftime("%Y%m%d")))])
    elif roles.is_admin:
        rows.append([btn(t(lang, "common.back"), AdminCb(s="bk", a="list"))])
    return "\n".join(lines), kb(rows)


async def render_card(
    target: Target,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
    booking_id: int,
    *,
    note: str | None = None,
) -> bool:
    try:
        async with read_tx(sm) as s:
            b = await bookings.get_booking_for_actor(
                s, booking_id=booking_id, actor_user_id=user.id
            )
    except ServiceError as e:
        if isinstance(target, CallbackQuery):
            await views.alert(target, views.err_text(lang, e))
        else:
            await target.answer(views.err_text(lang, e))
        return False
    text, markup = await card_view(sm, user, roles, lang, b, note=note)
    await screen.render(target, None, text, markup)
    return True


async def _digest_resolved(call: CallbackQuery, bid: int, note: str) -> None:
    """Drop the resolved booking's row from the nightly digest, keep the rest."""
    msg = call.message
    if isinstance(msg, Message) and msg.reply_markup is not None:
        rows = []
        for row in msg.reply_markup.inline_keyboard:
            keep = True
            for b in row:
                try:
                    cb = BookingCb.unpack(b.callback_data or "")
                except (ValueError, TypeError):
                    continue
                if cb.id == bid:
                    keep = False
            if keep:
                rows.append(row)
        try:
            await msg.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
            )
        except TelegramAPIError:
            pass
    await views.alert(call, note)


@router.callback_query(BookingCb.filter(F.a.in_(STAFF_ACTIONS)), IsStaff(), ANY)
async def on_staff_action(
    call: CallbackQuery,
    callback_data: BookingCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    a, bid = callback_data.a, callback_data.id
    digest = a in ("dd", "dn")
    if digest:
        a = "done" if a == "dd" else "ns"
    if a == "card":
        await render_card(call, sm, user, roles, lang, bid)
        return
    if a == "scx":
        try:
            async with read_tx(sm) as s:
                b = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=user.id)
        except ServiceError as e:
            await views.alert(call, views.err_text(lang, e))
            return
        if b.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await views.stale(call, lang)
            await render_card(call, sm, user, roles, lang, bid)
            return
        await state.set_state(StaffCancelFlow.reason_text)
        await state.update_data(booking_id=bid)
        text = (
            t(lang, "barber.cancel_reason_prompt")
            + "\n\n"
            + views.booking_text(b, lang, staff=True)
        )
        await screen.render(call, state, text, kb([[btn(t(lang, "common.cancel"), HubCb(a="cx"))]]))
        return
    try:
        if a == "done":
            async with write_tx(sm) as s:
                r = await bookings.complete_booking(s, booking_id=bid, actor_user_id=user.id)
            notes = [t(lang, "barber.done_ok")]
            if r.bonus_failed:
                notes.append(t(lang, "barber.bonus_failed", id=bid))
            elif r.redeemed:
                notes.append(t(lang, "barber.bonus_applied"))
            if r.needs_visit_confirm:
                notes.append(t(lang, "barber.visit_pending"))
            if digest:
                await _digest_resolved(call, bid, "\n".join(notes))
            else:
                await render_card(call, sm, user, roles, lang, bid, note="\n".join(notes))
            await notifier.booking_completed(r)
        else:  # ns
            async with write_tx(sm) as s:
                r = await bookings.mark_no_show(s, booking_id=bid, actor_user_id=user.id)
            note = t(lang, "barber.no_show_ok")
            if r.reversed:
                note += "\n" + t(lang, "barber.reversed", n=r.reversed)
            if digest:
                await _digest_resolved(call, bid, note)
            else:
                await render_card(call, sm, user, roles, lang, bid, note=note)
            await notifier.booking_no_show(r)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


@router.message(StateFilter(StaffCancelFlow.reason_text), F.text, IsStaff())
async def on_staff_cancel_reason(
    message: Message,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await state.get_data()
    bid = data.get("booking_id")
    try:
        reason = (validation.validate_comment(message.text or "") or "")[:200]
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    if not reason or not bid:
        await message.answer(t(lang, "barber.cancel_reason_prompt"))
        return
    try:
        async with write_tx(sm) as s:
            b = await bookings.cancel_by_staff(
                s, booking_id=int(bid), actor_user_id=user.id, reason=reason
            )
    except ServiceError as e:
        await state.clear()
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    # the prompt's ❌ button must not stay usable after the cancel is done
    await screen.strip_keyboard(message.bot, message.chat.id, data.get("msg_id"))  # type: ignore[arg-type]
    await render_card(message, sm, user, roles, lang, b.id, note=t(lang, "barber.cancelled_ok"))
    await notifier.booking_cancelled(b, by_client=False)


@router.message(StateFilter(StaffCancelFlow.reason_text))
async def on_staff_cancel_other(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
