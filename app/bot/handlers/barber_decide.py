"""Accept / reject (with reason) / propose another time [#20][#21].

Admins may decide too (fallback targets). A decision on a booking that has
already moved on refreshes the message to its current state (stale handling).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import BookingCb, HubCb, ProposeCb, RejectCb
from app.bot.filters import IsStaff
from app.bot.handlers.barber_queue import render_card
from app.bot.i18n import fmt_dt, h, t
from app.bot.keyboards import barber as bkb
from app.bot.keyboards.common import btn, kb
from app.bot.notifier import Notifier
from app.bot.states import RejectFlow
from app.db.enums import Awaiting, BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, slots, validation
from app.services.errors import InputError, InvalidTransitionError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="barber_decide")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(IsStaff())
router.message.filter(IsStaff())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")

DECISION_ACTIONS = ("acc", "rej", "prop", "dback")


async def _load_pending(
    call: CallbackQuery, sm: SM, user: Any, roles: Any, lang: str, bid: int
) -> Any | None:
    """The booking if it still waits for a staff decision; otherwise refresh the message."""
    try:
        async with read_tx(sm) as s:
            b = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=user.id)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return None
    if b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER:
        return b
    await views.stale(call, lang)
    await render_card(call, sm, user, roles, lang, bid)
    return None


async def _set_markup(call: CallbackQuery, markup: Any) -> None:
    if isinstance(call.message, Message):
        try:
            await call.message.edit_reply_markup(reply_markup=markup)
        except TelegramAPIError:
            pass
    await screen.safe_answer(call)


@router.callback_query(BookingCb.filter(F.a.in_(DECISION_ACTIONS)), ANY)
async def on_decision(
    call: CallbackQuery,
    callback_data: BookingCb,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    a, bid = callback_data.a, callback_data.id
    if a == "acc":
        try:
            async with write_tx(sm) as s:
                b = await bookings.confirm_booking(s, booking_id=bid, actor_user_id=user.id)
        except InvalidTransitionError as e:
            await views.alert(call, views.err_text(lang, e))
            await render_card(call, sm, user, roles, lang, bid)
            return
        except ServiceError as e:
            await views.alert(call, views.err_text(lang, e))
            return
        await render_card(
            call,
            sm,
            user,
            roles,
            lang,
            bid,
            note=t(lang, "notify.outcome.confirmed", name=h(b.barber_name)),
        )
        # this message now shows the full card: don't overwrite it with the outcome text
        skip = (call.message.chat.id, call.message.message_id) if call.message else None
        await notifier.booking_confirmed(b, skip=skip)
        return

    b = await _load_pending(call, sm, user, roles, lang, bid)
    if b is None:
        return
    if a == "rej":
        await _set_markup(call, bkb.reject_reasons_kb(bid, lang))
    elif a == "dback":
        await _set_markup(call, bkb.decision_kb(bid, lang))
    elif a == "prop":
        try:
            async with read_tx(sm) as s:
                options = await slots.list_proposal_slots(
                    s, booking_id=bid, actor_user_id=user.id, limit=8
                )
        except ServiceError as e:
            await views.alert(call, views.err_text(lang, e))
            return
        if not options:
            await views.alert(call, t(lang, "barber.no_proposals"))
            return
        if b.proposal_count >= 2:
            await views.alert(call, t(lang, "err.proposal_limit"))
            return
        await _set_markup(call, bkb.proposal_kb(bid, options, lang))


async def _do_reject(sm: SM, user: Any, bid: int, reason: str | None) -> Any:
    async with write_tx(sm) as s:
        return await bookings.reject_booking(
            s, booking_id=bid, actor_user_id=user.id, reason=reason
        )


@router.callback_query(RejectCb.filter(), ANY)
async def on_reject_reason(
    call: CallbackQuery,
    callback_data: RejectCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    bid = callback_data.id
    b = await _load_pending(call, sm, user, roles, lang, bid)
    if b is None:
        return
    r = callback_data.r
    if r == "other":
        await state.set_state(RejectFlow.reason_text)
        await state.update_data(booking_id=bid)
        await screen.safe_answer(call)
        assert call.message is not None
        prompt = await call.bot.send_message(  # type: ignore[union-attr]
            call.from_user.id,
            t(
                lang,
                "barber.reject_reason_prompt",
                when=fmt_dt(b.start_at, lang),
                name=h(b.client_name),
            ),
            reply_markup=kb([[btn(t(lang, "common.cancel"), HubCb(a="cx"))]]),
        )
        await state.update_data(msg_id=prompt.message_id, decision_msg_id=call.message.message_id)
        return
    if r not in ("busy", "sick", "none"):
        await views.stale(call, lang)
        return
    try:
        b = await _do_reject(sm, user, bid, None if r == "none" else r)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    await render_card(
        call,
        sm,
        user,
        roles,
        lang,
        bid,
        note=t(lang, "notify.outcome.rejected", name=h(b.barber_name)),
    )
    await notifier.booking_rejected(b)


@router.message(StateFilter(RejectFlow.reason_text), F.text)
async def on_reject_text(
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
        await message.answer(t(lang, "common.send_text"))
        return
    await state.clear()
    try:
        b = await _do_reject(sm, user, int(bid), reason)
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await screen.strip_keyboard(message.bot, message.chat.id, data.get("msg_id"))  # type: ignore[arg-type]
    await render_card(
        message,
        sm,
        user,
        roles,
        lang,
        b.id,
        note=t(lang, "notify.outcome.rejected", name=h(b.barber_name)),
    )
    await notifier.booking_rejected(b)


@router.message(StateFilter(RejectFlow.reason_text))
async def on_reject_other(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))


@router.callback_query(ProposeCb.filter(), ANY)
async def on_propose(
    call: CallbackQuery,
    callback_data: ProposeCb,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    bid = callback_data.id
    day = views.parse_day(callback_data.d)
    tm = views.parse_hhmm(callback_data.t)
    if day is None or tm is None:
        await views.stale(call, lang)
        return
    b = await _load_pending(call, sm, user, roles, lang, bid)
    if b is None:
        return
    new_start = clock.local_dt(day, tm)
    try:
        async with write_tx(sm) as s:
            r = await bookings.propose_time(
                s, booking_id=bid, actor_user_id=user.id, new_start_at=new_start
            )
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    note = t(lang, "barber.proposal_sent", when=fmt_dt(r.booking.start_at, lang))
    await render_card(call, sm, user, roles, lang, bid, note=note)
    await notifier.booking_proposed(r)
