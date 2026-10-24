"""Walk-in / phone booking by the barber [#26]: service → day → slot (lead time
ignored) → name → phone (optional) → confirm. Linking to a bot client happens
only on a single verified-phone match [D3]."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import ManualCb
from app.bot.filters import IsBarber
from app.bot.handlers.barber_queue import render_card
from app.bot.i18n import fmt_date, fmt_day_btn, fmt_dt, fmt_price, h, t
from app.bot.keyboards.client import service_label
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.states import ManualBookingFlow
from app.db.uow import read_tx, write_tx
from app.services import barbers, catalog, clock, schedule, slots, validation
from app.services.errors import InputError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="barber_manual")
router.message.filter(F.chat.type == "private")
router.message.filter(IsBarber())
router.callback_query.filter(IsBarber())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message

MANUAL_DAYS = 7


def _abort_row(lang: str) -> list[Any]:
    return [btn(t(lang, "common.cancel"), ManualCb(a="abort"))]


async def start_manual(target: Target, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    await state.clear()
    async with read_tx(sm) as s:
        offered = await catalog.list_barber_services(s, roles.barber_id)
        has_services = await catalog.shop_has_services(s)
    items = [rs for rs in offered if rs.service_id] if has_services else []
    await state.update_data(svc_none=not items)
    if not items:
        await _show_days(target, state, lang, 0)
        return
    rows = [
        [
            btn(
                service_label(rs.name or "", rs.price_uzs, rs.duration_min, lang),
                ManualCb(a="svc", s=rs.service_id or 0),
            )
        ]
        for rs in items
    ]
    rows.append(_abort_row(lang))
    await state.set_state(ManualBookingFlow.service)
    await screen.render(target, state, t(lang, "manual.choose_service"), kb(rows))


async def _show_days(target: Target, state: FSMContext, lang: str, service_id: int) -> None:
    today = clock.local_today()
    days = [today + timedelta(days=i) for i in range(MANUAL_DAYS)]
    buttons = [
        btn(fmt_day_btn(d, today, lang), ManualCb(a="day", s=service_id, d=d.strftime("%Y%m%d")))
        for d in days
    ]
    rows = chunk(buttons, 3)
    rows.append(_abort_row(lang))
    await state.set_state(ManualBookingFlow.day)
    await state.update_data(service_id=service_id or None)
    await screen.render(target, state, t(lang, "manual.choose_day"), kb(rows))


async def _manual_slots(
    sm: SM, barber_id: int, service_id: int | None, day: date
) -> list[datetime]:
    """Free starts with lead time ignored (walk-ins may start now)."""
    now = clock.now_utc()
    async with read_tx(sm) as s:
        barber = await barbers.get_barber(s, barber_id)
        rs = await catalog.resolve_service(s, barber_id=barber_id, service_id=service_id)
        week = await schedule.get_week_hours(s, barber_id)
        wh = next((w for w in week if w.weekday == day.weekday()), None)
        if wh is None:
            return []
        earliest = clock.floor_to_grid(now) - timedelta(minutes=15)
        ws = clock.local_dt(day, wh.start_time)
        we = clock.local_dt(day, wh.end_time)
        step = timedelta(minutes=barber.slot_step_min)
        cands: list[datetime] = []
        cur = ws
        while cur < we:
            cands.append(cur)
            cur += step
        if ws <= clock.floor_to_grid(now) < we and clock.floor_to_grid(now) not in cands:
            cands.append(clock.floor_to_grid(now))  # "right now" for a walk-in
        out = []
        for c in sorted(cands):
            if c < earliest or c + timedelta(minutes=rs.duration_min) > we:
                continue
            if await slots.is_slot_free(
                s,
                barber_id=barber_id,
                start_at=c,
                duration_min=rs.duration_min,
                buffer_min=barber.buffer_min,
                ignore_lead_time=True,
            ):
                out.append(c)
    return out


async def _check(
    call: CallbackQuery, state: FSMContext, lang: str, *allowed: Any
) -> dict[str, Any] | None:
    current = await state.get_state()
    data = await state.get_data()
    if current not in {s.state for s in allowed}:
        await screen.safe_answer(call, t(lang, "common.session_expired"), alert=True)
        await screen.render(call, None, t(lang, "common.session_expired"))
        return None
    if not isinstance(call.message, Message) or call.message.message_id != data.get("msg_id"):
        await views.stale(call, lang)
        return None
    return data


@router.callback_query(ManualCb.filter(F.a == "abort"), ANY)
async def on_abort(call: CallbackQuery, state: FSMContext, lang: str) -> None:
    await state.clear()
    await screen.render(call, None, t(lang, "common.cancelled"))


@router.callback_query(ManualCb.filter(F.a == "svc"), ANY)
async def on_service(
    call: CallbackQuery, callback_data: ManualCb, state: FSMContext, sm: SM, roles: Any, lang: str
) -> None:
    if await _check(call, state, lang, ManualBookingFlow.service) is None:
        return
    async with read_tx(sm) as s:
        offered = {rs.service_id for rs in await catalog.list_barber_services(s, roles.barber_id)}
    if callback_data.s not in offered:
        await views.stale(call, lang)
        return
    await _show_days(call, state, lang, callback_data.s)


@router.callback_query(ManualCb.filter(F.a == "day"), ANY)
async def on_day(
    call: CallbackQuery, callback_data: ManualCb, state: FSMContext, sm: SM, roles: Any, lang: str
) -> None:
    data = await _check(call, state, lang, ManualBookingFlow.day, ManualBookingFlow.slot)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    if day is None or (data.get("service_id") or 0) != callback_data.s:
        await views.stale(call, lang)
        return
    try:
        free = await _manual_slots(sm, roles.barber_id, callback_data.s or None, day)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    rows = []
    for x in free:
        loc = clock.to_local(x)
        rows.append(
            btn(
                loc.strftime("%H:%M"),
                ManualCb(a="slot", s=callback_data.s, d=callback_data.d, t=loc.strftime("%H%M")),
            )
        )
    kb_rows = chunk(rows, 4)
    kb_rows.append(
        [btn(t(lang, "common.back"), ManualCb(a="back_days", s=callback_data.s))] + _abort_row(lang)
    )
    text = (
        t(lang, "manual.choose_slot", day=fmt_date(day, lang))
        if free
        else t(lang, "manual.no_slots", day=fmt_date(day, lang))
    )
    await state.set_state(ManualBookingFlow.slot)
    await screen.render(call, state, text, kb(kb_rows))


@router.callback_query(ManualCb.filter(F.a == "back_days"), ANY)
async def on_back_days(
    call: CallbackQuery, callback_data: ManualCb, state: FSMContext, lang: str
) -> None:
    if await _check(call, state, lang, ManualBookingFlow.slot, ManualBookingFlow.name) is None:
        return
    await _show_days(call, state, lang, callback_data.s)


@router.callback_query(ManualCb.filter(F.a == "slot"), ANY)
async def on_slot(
    call: CallbackQuery, callback_data: ManualCb, state: FSMContext, lang: str
) -> None:
    data = await _check(call, state, lang, ManualBookingFlow.slot)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    tm = views.parse_hhmm(callback_data.t)
    if day is None or tm is None or (data.get("service_id") or 0) != callback_data.s:
        await views.stale(call, lang)
        return
    start = clock.local_dt(day, tm)
    await state.update_data(start_at=start.isoformat())
    await state.set_state(ManualBookingFlow.name)
    text = t(lang, "manual.name_prompt", when=fmt_dt(start, lang))
    await screen.render(call, state, text, kb([_abort_row(lang)]))


@router.message(StateFilter(ManualBookingFlow.name), F.text)
async def on_name(message: Message, state: FSMContext, lang: str) -> None:
    try:
        name = validation.validate_name(message.text or "")
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.update_data(client_name=name)
    await state.set_state(ManualBookingFlow.phone)
    rows = [[btn(t(lang, "manual.skip_phone"), ManualCb(a="skipphone"))], _abort_row(lang)]
    await screen.render(message, state, t(lang, "manual.phone_prompt", name=h(name)), kb(rows))


@router.message(StateFilter(ManualBookingFlow.phone), F.text)
async def on_phone(message: Message, state: FSMContext, sm: SM, lang: str, roles: Any) -> None:
    try:
        phone = validation.normalize_phone(message.text or "")
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.update_data(client_phone=phone)
    await _show_confirm(message, state, sm, lang, roles.barber_id)


@router.message(StateFilter(ManualBookingFlow.phone), F.contact)
async def on_phone_contact(
    message: Message, state: FSMContext, sm: SM, lang: str, roles: Any
) -> None:
    assert message.contact is not None
    try:
        phone = validation.normalize_phone(message.contact.phone_number)
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.update_data(client_phone=phone)
    await _show_confirm(message, state, sm, lang, roles.barber_id)


@router.callback_query(ManualCb.filter(F.a == "skipphone"), ANY)
async def on_skip_phone(
    call: CallbackQuery, state: FSMContext, sm: SM, lang: str, roles: Any
) -> None:
    if await _check(call, state, lang, ManualBookingFlow.phone) is None:
        return
    await state.update_data(client_phone=None)
    await _show_confirm(call, state, sm, lang, roles.barber_id)


async def _show_confirm(
    target: Target, state: FSMContext, sm: SM, lang: str, barber_id: int | None
) -> None:
    data = await state.get_data()
    start = datetime.fromisoformat(data["start_at"])
    lines = [t(lang, "manual.summary"), ""]
    lines.append(t(lang, "card.when", when=fmt_dt(start, lang)))
    if data.get("service_id") and barber_id is not None:
        try:  # the barber's own price/duration, as create_manual_booking will save it
            async with read_tx(sm) as s:
                rs = await catalog.resolve_service(
                    s, barber_id=barber_id, service_id=data["service_id"]
                )
        except ServiceError:
            rs = None
        if rs is not None:
            lines.append(t(lang, "card.service", name=h(rs.name), min=rs.duration_min))
            lines.append(t(lang, "card.price", price=fmt_price(rs.price_uzs, lang)))
    lines.append(t(lang, "card.client_name", name=h(data.get("client_name"))))
    lines.append(t(lang, "card.phone", phone=h(data.get("client_phone")) or "—"))
    rows = [[btn(t(lang, "book.confirm_btn"), ManualCb(a="confirm"))], _abort_row(lang)]
    await state.set_state(ManualBookingFlow.confirm)
    await screen.render(target, state, "\n".join(lines), kb(rows))


@router.callback_query(ManualCb.filter(F.a == "confirm"), ANY)
async def on_confirm(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    data = await _check(call, state, lang, ManualBookingFlow.confirm)
    if data is None:
        return
    try:
        async with write_tx(sm) as s:
            b = await _create(s, user, roles, data)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    await state.clear()
    note = (
        t(lang, "manual.created")
        + "\n"
        + (t(lang, "manual.linked") if b.client_id else t(lang, "manual.unlinked"))
    )
    await render_card(call, sm, user, roles, lang, b.id, note=note)


async def _create(s: AsyncSession, user: Any, roles: Any, data: dict[str, Any]) -> Any:
    from app.services import bookings

    return await bookings.create_manual_booking(
        s,
        actor_user_id=user.id,
        barber_id=roles.barber_id,
        service_id=data.get("service_id"),
        start_at=datetime.fromisoformat(data["start_at"]),
        client_name=data["client_name"],
        client_phone=data.get("client_phone"),
    )


@router.message(StateFilter(ManualBookingFlow.name, ManualBookingFlow.phone))
async def on_text_expected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
