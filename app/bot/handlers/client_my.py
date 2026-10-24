"""My bookings: list, cancel / withdraw, proposal answers, "Kelaman", manual-visit
confirmation, and the rebook entry (RebookCb)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import BookingCb, HubCb, RebookCb
from app.bot.i18n import fmt_dt, fmt_dt_short, fmt_price, fmt_stamps, h, t
from app.bot.keyboards.client import rebook_kb, result_kb
from app.bot.keyboards.common import btn, home_btn, kb
from app.bot.notifier import Notifier
from app.db.enums import Awaiting, BookingStatus, LoyaltyMode
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, clock, settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="client_my")
router.message.filter(F.chat.type == "private")

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")

CLIENT_ACTIONS = ("ccx", "ccy", "wd", "pya", "pno", "ack", "vy", "vn")


async def show_my(
    target: CallbackQuery | Message, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    async with read_tx(sm) as s:
        res = await bookings.list_client_bookings(s, client_user_id=user.id)
    lines = [t(lang, "my.title")]
    rows = []
    if res.upcoming:
        lines.append("")
        lines.append(t(lang, "my.upcoming"))
        for b in res.upcoming:
            lines.append("")
            lines.extend(views.booking_lines(b, lang, staff=False))
            when = fmt_dt_short(b.start_at, lang)
            if b.status == BookingStatus.CONFIRMED:
                rows.append([btn(f"{t(lang, 'my.cancel')} · {when}", BookingCb(a="ccx", id=b.id))])
            elif b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER:
                rows.append([btn(f"{t(lang, 'my.withdraw')} · {when}", BookingCb(a="wd", id=b.id))])
            elif b.status == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT:
                rows.append(
                    [
                        btn(f"{t(lang, 'my.proposal_yes')} · {when}", BookingCb(a="pya", id=b.id)),
                        btn(t(lang, "my.proposal_no"), BookingCb(a="pno", id=b.id)),
                    ]
                )
    else:
        lines.append("")
        lines.append(t(lang, "my.none"))
    if res.past:
        lines.append("")
        lines.append(t(lang, "my.past"))
        for b in res.past:
            svc = f" · {h(b.service_name)}" if b.service_name else ""
            lines.append(
                f"{views.STATUS_ICON.get(b.status, '•')} {fmt_dt_short(b.start_at, lang)} · {h(b.barber_name)}{svc}"
            )
        last_done = next((b for b in res.past if b.status == BookingStatus.COMPLETED), None)
        if last_done is not None:
            rows.append(
                [
                    btn(
                        t(lang, "notify.rebook_btn"),
                        RebookCb(b=last_done.barber_id, s=last_done.service_id or 0),
                    )
                ]
            )
    rows.append([btn(t(lang, "hub.book"), HubCb(a="book")), home_btn(lang)])
    await screen.render(target, state, "\n".join(lines), kb(rows))


async def _load(call: CallbackQuery, sm: SM, user: Any, lang: str, booking_id: int) -> Any | None:
    try:
        async with read_tx(sm) as s:
            b = await bookings.get_booking_for_actor(
                s, booking_id=booking_id, actor_user_id=user.id
            )
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return None
    if b.client_id != user.id:
        await views.stale(call, lang)
        return None
    return b


async def _replace(call: CallbackQuery, text: str, markup: Any = None) -> None:
    """Edit the message the button sat on (wizard result, reminder, proposal…)."""
    await screen.render(call, None, text, markup)


@router.callback_query(BookingCb.filter(F.a.in_(CLIENT_ACTIONS)), ANY)
async def on_client_action(
    call: CallbackQuery,
    callback_data: BookingCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    a, bid = callback_data.a, callback_data.id
    b = await _load(call, sm, user, lang, bid)
    if b is None:
        return
    now = clock.now_utc()

    if a == "ccx":
        if b.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED) or b.start_at <= now:
            await views.alert(call, t(lang, "my.cannot_cancel"))
            return
        async with read_tx(sm) as s:
            shop = await settings.get_shop_settings(s)
        late = b.status == BookingStatus.CONFIRMED and b.start_at - now < timedelta(
            hours=shop.free_cancel_hours
        )
        text = t(lang, "my.cancel_confirm", when=fmt_dt(b.start_at, lang), name=h(b.barber_name))
        if late:
            text += "\n\n" + t(lang, "my.cancel_late_warn", hours=shop.free_cancel_hours)
        markup = kb(
            [
                [btn(t(lang, "my.cancel_yes"), BookingCb(a="ccy", id=b.id))],
                [btn(t(lang, "my.cancel_no"), HubCb(a="my"))],
            ]
        )
        await _replace(call, text, markup)
        return

    try:
        if a in ("ccy", "wd"):
            if a == "wd" and not (
                b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER
            ):
                await views.alert(call, t(lang, "my.cannot_cancel"))
                return
            was_active = b.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED)
            async with write_tx(sm) as s:
                res = await bookings.cancel_by_client(
                    s,
                    booking_id=bid,
                    client_user_id=user.id,
                    reason="withdrawn" if a == "wd" else None,
                )
            key = (
                "my.withdrawn"
                if a == "wd"
                else ("my.cancelled_late" if res.late else "my.cancelled")
            )
            await _replace(
                call,
                t(lang, key, when=fmt_dt(res.booking.start_at, lang)),
                rebook_kb(res.booking.barber_id, res.booking.service_id, lang),
            )
            if was_active:  # a repeated tap (another message) must not notify twice
                await notifier.booking_cancelled(res.booking, by_client=True)
        elif a in ("pya", "pno"):
            was_open = b.status == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT
            async with write_tx(sm) as s:
                nb = await bookings.answer_proposal(
                    s, booking_id=bid, client_user_id=user.id, accept=a == "pya"
                )
            if nb.status == BookingStatus.CONFIRMED:
                text = views.booking_text(
                    nb, lang, staff=False, title=t(lang, "my.proposal_accepted") + "\n"
                )
                await _replace(call, text, result_kb(nb, lang))
            else:
                await _replace(
                    call,
                    t(lang, "my.proposal_declined"),
                    rebook_kb(nb.barber_id, nb.service_id, lang),
                )
            if was_open:
                await notifier.proposal_answered(nb)
        elif a == "ack":
            async with write_tx(sm) as s:
                nb = await bookings.client_ack(s, booking_id=bid, client_user_id=user.id)
            text = t(lang, "my.ack_done", when=fmt_dt(nb.start_at, lang))
            await _replace(
                call, text, kb([[btn(t(lang, "my.cancel"), BookingCb(a="ccx", id=nb.id))]])
            )
        elif a in ("vy", "vn"):
            async with write_tx(sm) as s:
                r = await bookings.confirm_manual_visit(
                    s, booking_id=bid, client_user_id=user.id, yes=a == "vy"
                )
                st = await bonus.get_bonus_status(s, user.id, recent_limit=0) if a == "vy" else None
            if a == "vy":
                lines = [t(lang, "my.visit_yes")]
                if st is not None and st.enabled:
                    if st.mode == LoyaltyMode.STAMPS:
                        if r.earned:
                            lines.append(t(lang, "notify.stamp_earned", n=r.earned))
                        lines.append(
                            f"{fmt_stamps(st.progress, st.required)} {st.progress}/{st.required}"
                        )
                    else:
                        lines.append(
                            t(lang, "bonus.cash_balance", amount=fmt_price(st.available, lang))
                        )
                await _replace(call, "\n".join(lines))
            else:
                await _replace(call, t(lang, "my.visit_no"))
                if b.visit_confirmed is None:
                    await notifier.manual_visit_denied(r.booking)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return


@router.callback_query(RebookCb.filter(), ANY)
async def on_rebook(
    call: CallbackQuery, callback_data: RebookCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    from app.bot.handlers import client_booking

    # keep the notification text; the wizard opens as a new message below it
    await screen.strip_keyboard(
        call.bot, call.from_user.id, call.message.message_id if call.message else None
    )  # type: ignore[arg-type]
    await screen.safe_answer(call)
    target: CallbackQuery | Message = call.message if isinstance(call.message, Message) else call
    await client_booking.start_booking(
        target,
        state,
        sm,
        user,
        lang,
        entry="rebook",
        barber_id=callback_data.b,
        service_id=callback_data.s or None,
    )
