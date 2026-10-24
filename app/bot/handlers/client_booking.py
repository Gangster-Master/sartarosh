"""Client booking wizard (ARCHITECTURE.md §6.1, BookingFlow).

One message, edited in place; ``nav`` is a back stack of step names so ⬅️ goes
to the previous step with earlier choices kept [#8]. Every wizard callback
checks that it came from the current wizard message (``msg_id``) [E2][#10] and
that a wizard is in progress at all (otherwise ``session_expired`` + hub).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import BarberCardCb, BarberCb, CalCb, DayCb, SlotCb, SvcCb, WizCb
from app.bot.i18n import fmt_date, fmt_dt, fmt_price, h, t
from app.bot.keyboards import client as ckb
from app.bot.keyboards.calendar import month_kb, parse_ym, ym
from app.bot.keyboards.common import btn, contact_request, kb, menu_for, wiz_nav_row
from app.bot.notifier import Notifier
from app.bot.states import BookingFlow
from app.db.uow import read_tx, write_tx
from app.services import (
    barbers,
    bonus,
    bookings,
    catalog,
    clock,
    settings,
    slots,
    users,
    validation,
)
from app.services.errors import (
    ConsentRequiredError,
    InputError,
    InsufficientBonusError,
    InvalidNameError,
    NotFoundError,
    PhoneRequiredError,
    ServiceError,
    SlotTakenError,
    SlotUnavailableError,
)

log = logging.getLogger(__name__)

router = Router(name="client_booking")
router.message.filter(F.chat.type == "private")

SM = async_sessionmaker[AsyncSession]
Target = CallbackQuery | Message

STATES = {
    "service": BookingFlow.service,
    "barber": BookingFlow.barber,
    "barber_card": BookingFlow.barber_card,
    "day": BookingFlow.day,
    "calendar": BookingFlow.calendar,
    "slot": BookingFlow.slot,
    "comment": BookingFlow.comment,
    "contact": BookingFlow.contact,
    "name_input": BookingFlow.name_input,
    "confirm": BookingFlow.confirm,
}


# ===================================================================== entry


async def start_booking(
    target: Target,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    *,
    entry: str,
    barber_id: int | None = None,
    service_id: int | None = None,
) -> None:
    """Start BookingFlow. ``entry``: service_first | barber_first | rebook."""
    await state.clear()
    data: dict[str, Any] = {
        "nav": [],
        "entry": entry,
        "service_id": None,
        "barber_id": None,
        "barber_fixed": False,
        "day": None,
        "start_at": None,
        "comment": None,
        "use_bonus": False,
    }
    async with read_tx(sm) as s:
        has_services = await catalog.shop_has_services(s)
        barber = None
        if barber_id:
            try:
                barber = await barbers.get_barber(s, barber_id)
            except NotFoundError:
                barber = None
        barber_services = (
            await catalog.list_barber_services(s, barber.id) if barber and barber.is_public else []
        )
        bookable_ids = (
            {x.id for x in await catalog.list_bookable_services(s)} if has_services else set()
        )

    step = "service" if has_services else "barber"
    if entry in ("barber_first", "rebook") and barber is not None and barber.is_public:
        data["barber_id"] = barber.id
        data["barber_fixed"] = True
        offered = {x.service_id for x in barber_services if x.service_id}
        if not has_services or not offered:
            step = "day"
        elif service_id and service_id in offered:
            data["service_id"] = service_id
            step = "day"
        elif len(offered) == 1:
            data["service_id"] = next(iter(offered))
            step = "day"
        else:
            step = "service"
    elif entry == "rebook" and service_id and service_id in bookable_ids:
        # the barber is no longer public: keep the service, start at the barber step
        data["service_id"] = service_id
        step = "barber"
        data["entry"] = "service_first"
    else:
        data["entry"] = "service_first"

    data["step"] = step
    await state.set_data(data)
    await _draw(target, state, sm, user, lang, step)


# ===================================================================== drawing


async def _go(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, step: str, *, push: bool = True
) -> None:
    data = await state.get_data()
    nav: list[str] = list(data.get("nav") or [])
    cur = data.get("step")
    if push and cur and cur != step:
        nav.append(cur)
    await state.update_data(nav=nav, step=step)
    await _draw(target, state, sm, user, lang, step)


async def _draw(target: Target, state: FSMContext, sm: SM, user: Any, lang: str, step: str) -> None:
    await state.set_state(STATES[step])
    await state.update_data(step=step)
    data = await state.get_data()
    drawer = {
        "service": _draw_service,
        "barber": _draw_barber,
        "barber_card": _draw_barber_card,
        "day": _draw_day,
        "calendar": _draw_calendar,
        "slot": _draw_slot,
        "comment": _draw_comment,
        "contact": _draw_contact,
        "name_input": _draw_name,
        "confirm": _draw_confirm,
    }[step]
    await drawer(target, state, sm, user, lang, data)


def _has_back(data: dict[str, Any]) -> bool:
    return bool(data.get("nav"))


async def _context_line(sm: SM, data: dict[str, Any], lang: str) -> str:
    """'✂️ Soch olish · 💈 Ali' — the choices made so far."""
    parts = []
    async with read_tx(sm) as s:
        if data.get("service_id"):
            for x in await catalog.list_services(s, active_only=False):
                if x.id == data["service_id"]:
                    parts.append(f"✂️ {h(x.name)}")
                    break
        if data.get("barber_id"):
            try:
                b = await barbers.get_barber(s, data["barber_id"])
                parts.append(f"💈 {h(b.display_name)}")
            except NotFoundError:
                pass
        elif data.get("step") not in ("service", "barber", "barber_card") and data.get(
            "barber_chosen_any"
        ):
            parts.append(t(lang, "book.any_barber_short"))
    return " · ".join(parts)


async def _draw_service(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    items: list[tuple[int, str]] = []
    async with read_tx(sm) as s:
        if data.get("barber_fixed") and data.get("barber_id"):
            for rs in await catalog.list_barber_services(s, data["barber_id"]):
                if rs.service_id:
                    items.append(
                        (
                            rs.service_id,
                            ckb.service_label(rs.name or "", rs.price_uzs, rs.duration_min, lang),
                        )
                    )
        else:
            for x in await catalog.list_bookable_services(s):
                items.append((x.id, ckb.service_label(x.name, x.price_uzs, x.duration_min, lang)))
    if not items:
        await screen.render(
            target,
            state,
            t(lang, "book.no_services"),
            kb([wiz_nav_row(lang, back=_has_back(data))]),
        )
        return
    head = await _context_line(sm, data, lang)
    text = (head + "\n\n" if head else "") + t(lang, "book.choose_service")
    await screen.render(target, state, text, ckb.services_kb(items, lang, back=_has_back(data)))


async def _draw_barber(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    async with read_tx(sm) as s:
        items = await catalog.list_barbers_for_service(s, service_id=data.get("service_id"))
    if not items:
        await screen.render(
            target, state, t(lang, "book.no_barbers"), kb([wiz_nav_row(lang, back=_has_back(data))])
        )
        return
    head = await _context_line(sm, data, lang)
    text = (head + "\n\n" if head else "") + t(lang, "book.choose_barber")
    await screen.render(target, state, text, ckb.barbers_kb(items, lang, back=_has_back(data)))


async def _draw_barber_card(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    from app.bot.handlers.client_info import barber_card_content

    text, photo = await barber_card_content(sm, data["card_barber_id"], lang)
    markup = kb(
        [
            [btn(t(lang, "info.book_this"), BarberCardCb(a="book", b=data["card_barber_id"]))],
            [btn(t(lang, "common.back"), BarberCardCb(a="back", b=data["card_barber_id"]))],
        ]
    )
    await screen.render(target, state, text, markup, photo=photo)


async def _days(sm: SM, data: dict[str, Any]) -> list[Any]:
    async with read_tx(sm) as s:
        return await slots.get_available_days(
            s, barber_id=data.get("barber_id"), service_id=data.get("service_id")
        )


async def _draw_day(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    days = await _days(sm, data)
    head = await _context_line(sm, data, lang)
    if not any(d.has_free_slots for d in days):
        text = (head + "\n\n" if head else "") + t(lang, "book.no_days")
        await screen.render(target, state, text, kb([wiz_nav_row(lang, back=_has_back(data))]))
        return
    text = (head + "\n\n" if head else "") + t(lang, "book.choose_day")
    markup = ckb.days_kb(days, lang, back=_has_back(data), more=len(days) > ckb.QUICK_DAYS)
    await screen.render(target, state, text, markup)


async def _draw_calendar(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    days = await _days(sm, data)
    today = clock.local_today()
    first = days[0].day if days else today
    last = days[-1].day if days else today
    parsed = parse_ym(data.get("cal_month") or ym(first))
    y, m = parsed if parsed else (first.year, first.month)
    available = {d.day for d in days if d.is_working and d.has_free_slots}
    head = await _context_line(sm, data, lang)
    text = (head + "\n\n" if head else "") + t(lang, "book.choose_day_cal")
    await screen.render(
        target, state, text, month_kb(y, m, available=available, first=first, last=last, lang=lang)
    )


async def _free_slots(sm: SM, data: dict[str, Any], day: date) -> list[datetime]:
    async with read_tx(sm) as s:
        if data.get("barber_id"):
            return await slots.get_free_slots(
                s, barber_id=data["barber_id"], service_id=data.get("service_id"), day=day
            )
        return await slots.get_free_slots_any(s, service_id=data.get("service_id"), day=day)


async def _draw_slot(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    day = date.fromisoformat(data["day"])
    free = await _free_slots(sm, data, day)
    head = await _context_line(sm, data, lang)
    prefix = head + "\n\n" if head else ""
    if not free:
        text = prefix + t(lang, "book.no_slots", day=fmt_date(day, lang))
        await screen.render(target, state, text, kb([wiz_nav_row(lang)]))
        return
    text = prefix + t(lang, "book.choose_slot", day=fmt_date(day, lang))
    markup = ckb.slots_kb(
        free,
        barber_id=data.get("barber_id") or 0,
        service_id=data.get("service_id") or 0,
        day=day,
        lang=lang,
    )
    await screen.render(target, state, text, markup)


async def _draw_comment(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    text = t(lang, "book.comment_prompt")
    if data.get("comment"):
        text += "\n\n" + t(lang, "card.comment", text=h(data["comment"]))
    await screen.render(target, state, text, ckb.comment_kb(lang))


async def _draw_contact(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    await screen.render(target, state, t(lang, "book.contact_wizard"), kb([wiz_nav_row(lang)]))
    bot: Bot = target.bot  # type: ignore[assignment]
    chat_id = target.chat.id if isinstance(target, Message) else target.from_user.id
    prompt = await screen.send_reply_kb(
        bot, chat_id, t(lang, "book.contact_prompt"), contact_request(lang)
    )
    if prompt is not None:
        await state.update_data(prompt_id=prompt.message_id)


async def _draw_name(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    async with read_tx(sm) as s:
        fresh = await users.get_user(s, user.id)
    text = t(lang, "book.name_prompt", name=h(fresh.display_name))
    await screen.render(target, state, text, kb([wiz_nav_row(lang)]))


async def _price_info(sm: SM, data: dict[str, Any]) -> tuple[str | None, int, int | None]:
    """(service name, price, duration) for the summary."""
    async with read_tx(sm) as s:
        if data.get("barber_id"):
            rs = await catalog.resolve_service(
                s, barber_id=data["barber_id"], service_id=data.get("service_id")
            )
            return rs.name, rs.price_uzs, rs.duration_min
        if data.get("service_id"):
            # "any barber": the chosen barber may override price/duration, so show the
            # lowest price ("from X"); the result screen shows the booked price.
            options = []
            for b in await catalog.list_barbers_for_service(s, service_id=data["service_id"]):
                try:
                    options.append(
                        await catalog.resolve_service(
                            s, barber_id=b.id, service_id=data["service_id"]
                        )
                    )
                except InputError:
                    continue
            if options:
                low = min(options, key=lambda r: (r.price_uzs, r.duration_min))
                varies = len({(r.price_uzs, r.duration_min) for r in options}) > 1
                data["_price_varies"] = varies
                return low.name, low.price_uzs, low.duration_min
            for x in await catalog.list_services(s, active_only=False):
                if x.id == data["service_id"]:
                    return x.name, x.price_uzs, x.duration_min
    return None, 0, None


async def _draw_confirm(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, data: dict[str, Any]
) -> None:
    start_at = datetime.fromisoformat(data["start_at"])
    try:
        svc_name, price, minutes = await _price_info(sm, data)
    except InputError:
        svc_name, price, minutes = None, 0, None
    async with read_tx(sm) as s:
        fresh = await users.get_user(s, user.id)
        barber_name = None
        if data.get("barber_id"):
            barber_name = (await barbers.get_barber(s, data["barber_id"])).display_name
        quote = await bonus.quote_redeem(s, user_id=user.id, price_uzs=price) if price > 0 else None
        soft = await users.is_soft_blocked(s, user.id)
    bonus_possible = bool(quote and quote.possible)
    use_bonus = bool(data.get("use_bonus")) and bonus_possible
    if bool(data.get("use_bonus")) != use_bonus:
        await state.update_data(use_bonus=use_bonus)

    lines = [t(lang, "book.summary_title"), ""]
    lines.append(
        t(
            lang,
            "card.barber",
            name=h(barber_name) if barber_name else t(lang, "book.any_barber_short"),
        )
    )
    if svc_name:
        lines.append(t(lang, "card.service", name=h(svc_name), min=minutes or 0))
    lines.append(t(lang, "card.when", when=fmt_dt(start_at, lang)))
    if price > 0:
        if use_bonus and quote is not None:
            lines.append(
                t(
                    lang,
                    "card.price_bonus",
                    price=fmt_price(quote.final_price_uzs, lang),
                    full=fmt_price(price, lang),
                )
            )
        elif data.get("_price_varies"):
            lines.append(t(lang, "card.price_from", price=fmt_price(price, lang)))
        else:
            lines.append(t(lang, "card.price", price=fmt_price(price, lang)))
            if bonus_possible and quote is not None:
                lines.append(
                    t(lang, "book.bonus_hint", price=fmt_price(quote.final_price_uzs, lang))
                )
    lines.append(t(lang, "book.name_line", name=h(fresh.display_name)))
    lines.append(t(lang, "card.phone", phone=h(fresh.phone) if fresh.phone else "—"))
    if data.get("comment"):
        lines.append(t(lang, "card.comment", text=h(data["comment"])))
    if soft:
        lines.extend(["", t(lang, "book.soft_block_warn")])
    markup = ckb.confirm_kb(lang, bonus_possible=bonus_possible, use_bonus=use_bonus)
    await screen.render(target, state, "\n".join(lines), markup)


# ===================================================================== guards


async def _wizard_data(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> dict[str, Any] | None:
    current = await state.get_state()
    data = await state.get_data()
    if not current or not current.startswith("BookingFlow:") or "msg_id" not in data:
        await screen.safe_answer(call, t(lang, "common.session_expired"), alert=True)
        await views.show_hub(call, state, sm, user, roles, lang)
        return None
    if not isinstance(call.message, Message) or call.message.message_id != data.get("msg_id"):
        await views.stale(call, lang)
        return None
    return data


async def cleanup_contact(bot: Bot, chat_id: int, state: FSMContext, roles: Any, lang: str) -> None:
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    if prompt_id:
        await screen.safe_delete(bot, chat_id, prompt_id)
        await state.update_data(prompt_id=None)
        await screen.send_reply_kb(
            bot, chat_id, t(lang, "common.menu_restored"), menu_for(roles, lang, "c")
        )


# ===================================================================== step handlers


@router.callback_query(SvcCb.filter())
async def on_service(
    call: CallbackQuery,
    callback_data: SvcCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    if data.get("step") != "service":
        await views.stale(call, lang)
        return
    sid = callback_data.s or None
    async with read_tx(sm) as s:
        if data.get("barber_fixed") and data.get("barber_id"):
            valid = {x.service_id for x in await catalog.list_barber_services(s, data["barber_id"])}
        else:
            valid = {x.id for x in await catalog.list_bookable_services(s)}
    if sid not in valid:
        await views.stale(call, lang)
        return
    await state.update_data(service_id=sid, day=None, start_at=None, use_bonus=False)
    next_step = "day" if data.get("barber_fixed") else "barber"
    await _go(call, state, sm, user, lang, next_step)


@router.callback_query(BarberCb.filter())
async def on_barber(
    call: CallbackQuery,
    callback_data: BarberCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    if data.get("step") != "barber":
        await views.stale(call, lang)
        return
    await _choose_barber(call, state, sm, user, roles, lang, data, callback_data.b or None)


async def _choose_barber(
    call: CallbackQuery,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
    data: dict[str, Any],
    barber_id: int | None,
) -> None:
    async with read_tx(sm) as s:
        allowed = {
            b.id
            for b in await catalog.list_barbers_for_service(s, service_id=data.get("service_id"))
        }
    if barber_id is not None and barber_id not in allowed:
        await views.alert(call, t(lang, "err.barber_inactive"))
        await _draw(call, state, sm, user, lang, "barber")
        return
    nav: list[str] = list(data.get("nav") or [])
    if data.get("step") == "barber_card" and nav and nav[-1] == "barber":
        nav.pop()  # booked from the card: "back" from the day step returns to the list once
    await state.update_data(
        barber_id=barber_id,
        barber_chosen_any=barber_id is None,
        day=None,
        start_at=None,
        step="barber",
        nav=nav,
    )
    await _go(call, state, sm, user, lang, "day")


@router.callback_query(BarberCardCb.filter())
async def on_barber_card_in_wizard(
    call: CallbackQuery,
    callback_data: BarberCardCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    current = await state.get_state()
    data = await state.get_data()
    in_wizard = current in (BookingFlow.barber.state, BookingFlow.barber_card.state)
    if (
        not in_wizard
        or not isinstance(call.message, Message)
        or call.message.message_id != data.get("msg_id")
    ):
        raise SkipHandler()  # a card outside the wizard: client_info handles it
    if callback_data.a == "view":
        await state.update_data(card_barber_id=callback_data.b)
        await _go(call, state, sm, user, lang, "barber_card")
    elif callback_data.a == "book":
        await _choose_barber(call, state, sm, user, roles, lang, data, callback_data.b)
    else:  # back
        await _back(call, state, sm, user, roles, lang)


@router.callback_query(DayCb.filter())
async def on_day(
    call: CallbackQuery,
    callback_data: DayCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    if data.get("step") != "day":
        await views.stale(call, lang)
        return
    await _pick_day(call, state, sm, user, lang, data, views.parse_day(callback_data.d))


async def _pick_day(
    call: CallbackQuery,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    data: dict[str, Any],
    day: date | None,
) -> None:
    if day is None:
        await views.stale(call, lang)
        return
    days = {d.day: d for d in await _days(sm, data)}
    info = days.get(day)
    if info is None:
        await views.stale(call, lang)
        return
    if not info.is_working or not info.has_free_slots:
        await views.alert(call, t(lang, "book.day_full_alert"))
        return
    await state.update_data(day=day.isoformat(), start_at=None)
    await _go(call, state, sm, user, lang, "slot")


@router.callback_query(CalCb.filter())
async def on_calendar(
    call: CallbackQuery,
    callback_data: CalCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    if data.get("step") not in ("day", "calendar"):
        await views.stale(call, lang)
        return
    if callback_data.a == "m":
        if parse_ym(callback_data.v) is None:
            await views.stale(call, lang)
            return
        await state.update_data(cal_month=callback_data.v)
        await _go(call, state, sm, user, lang, "calendar", push=data.get("step") == "day")
        return
    await _pick_day(call, state, sm, user, lang, data, views.parse_day(callback_data.v))


@router.callback_query(SlotCb.filter())
async def on_slot(
    call: CallbackQuery,
    callback_data: SlotCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    tm = views.parse_hhmm(callback_data.t)
    if (
        data.get("step") != "slot"
        or day is None
        or tm is None
        or data.get("day") != day.isoformat()
        or (data.get("barber_id") or 0) != callback_data.b
        or (data.get("service_id") or 0) != callback_data.s
    ):
        await views.stale(call, lang)
        return
    start_at = clock.local_dt(day, tm)
    if start_at not in await _free_slots(sm, data, day):
        await views.alert(call, t(lang, "err.slot_taken"))
        await _draw(call, state, sm, user, lang, "slot")
        return
    await state.update_data(start_at=start_at.isoformat())
    await _go(call, state, sm, user, lang, "comment")


async def _after_comment(target: Target, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    async with read_tx(sm) as s:
        fresh = await users.get_user(s, user.id)
    if not fresh.phone or not fresh.phone_verified:
        await _go(target, state, sm, user, lang, "contact")
        return
    if not _name_ok(fresh.display_name):
        await _go(target, state, sm, user, lang, "name_input")
        return
    await _go(target, state, sm, user, lang, "confirm")


def _name_ok(name: str | None) -> bool:
    try:
        validation.validate_name(name or "")
    except InvalidNameError:
        return False
    return True


@router.message(StateFilter(BookingFlow.comment), F.text)
async def on_comment_text(
    message: Message, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    try:
        comment = validation.validate_comment(message.text or "")
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.update_data(comment=comment)
    await _after_comment(message, state, sm, user, lang)


@router.message(StateFilter(BookingFlow.contact), F.contact)
async def on_contact(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    contact = message.contact
    assert contact is not None
    if contact.user_id != message.from_user.id:
        await message.answer(t(lang, "contact.only_own"))
        return
    try:
        async with write_tx(sm) as s:
            res = await users.set_verified_contact(
                s, user_id=user.id, raw_phone=contact.phone_number
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    bot: Bot = message.bot  # type: ignore[assignment]
    data = await state.get_data()
    await screen.safe_delete(bot, message.chat.id, data.get("prompt_id"))
    await screen.strip_keyboard(bot, message.chat.id, data.get("msg_id"))
    await state.update_data(prompt_id=None, msg_id=None)
    await screen.send_reply_kb(
        bot, message.chat.id, t(lang, "contact.saved"), menu_for(roles, lang, "c")
    )
    # the summary (or the name step) is a NEW message; the old wizard message is now stale [E2]
    next_step = "confirm" if _name_ok(res.user.display_name) else "name_input"
    await _go(message, state, sm, res.user, lang, next_step, push=False)


@router.message(StateFilter(BookingFlow.contact))
async def on_contact_other(message: Message, lang: str) -> None:
    await message.answer(t(lang, "contact.use_button"))


@router.message(StateFilter(BookingFlow.name_input), F.text)
async def on_name_text(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    try:
        async with write_tx(sm) as s:
            fresh = await users.set_full_name(s, user_id=user.id, name=message.text or "")
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    data = await state.get_data()
    nav = list(data.get("nav") or [])
    if nav and nav[-1] == "confirm":
        nav.pop()
    await state.update_data(nav=nav, step="confirm")
    await _draw(message, state, sm, fresh, lang, "confirm")


@router.message(StateFilter(BookingFlow.name_input, BookingFlow.comment))
async def on_text_expected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))


# ===================================================================== wizard navigation


@router.callback_query(WizCb.filter())
async def on_wiz(
    call: CallbackQuery,
    callback_data: WizCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _wizard_data(call, state, sm, user, roles, lang)
    if data is None:
        return
    a = callback_data.a
    step = data.get("step")
    if a == "back":
        await _back(call, state, sm, user, roles, lang)
    elif a == "cancel":
        await cleanup_contact(call.bot, call.from_user.id, state, roles, lang)  # type: ignore[arg-type]
        await state.clear()
        await views.show_hub(call, state, sm, user, roles, lang, prefix=t(lang, "book.cancelled"))
    elif a == "skip_comment" and step == "comment":
        await state.update_data(comment=None)
        await _after_comment(call, state, sm, user, lang)
    elif a in ("bonus_on", "bonus_off") and step == "confirm":
        await state.update_data(use_bonus=a == "bonus_on")
        await _draw(call, state, sm, user, lang, "confirm")
    elif a == "edit_name" and step == "confirm":
        await _go(call, state, sm, user, lang, "name_input")
    elif a == "confirm" and step == "confirm":
        await _confirm(call, state, sm, notifier, user, roles, lang, data)
    else:
        await views.stale(call, lang)


async def _back(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    data = await state.get_data()
    if data.get("step") == "contact":
        await cleanup_contact(call.bot, call.from_user.id, state, roles, lang)  # type: ignore[arg-type]
    nav = list(data.get("nav") or [])
    if not nav:
        await state.clear()
        await views.show_hub(call, state, sm, user, roles, lang)
        return
    prev = nav.pop()
    await state.update_data(nav=nav, step=prev)
    await _draw(call, state, sm, user, lang, prev)


async def _back_to_slots(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    data = await state.get_data()
    nav = list(data.get("nav") or [])
    if "slot" in nav:
        nav = nav[: nav.index("slot")]
    await state.update_data(nav=nav, step="slot", start_at=None)
    await _draw(call, state, sm, user, lang, "slot")


async def _confirm(
    call: CallbackQuery,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
    data: dict[str, Any],
) -> None:
    start_at = datetime.fromisoformat(data["start_at"])
    try:
        async with write_tx(sm) as s:
            res = await bookings.create_booking(
                s,
                client_user_id=user.id,
                barber_id=data.get("barber_id"),
                service_id=data.get("service_id"),
                start_at=start_at,
                comment=data.get("comment"),
                use_bonus=bool(data.get("use_bonus")),
            )
    except (SlotTakenError, SlotUnavailableError) as e:
        await views.alert(call, views.err_text(lang, e))
        await _back_to_slots(call, state, sm, user, lang)
        return
    except InvalidNameError as e:
        await views.alert(call, views.err_text(lang, e))
        await _go(call, state, sm, user, lang, "name_input")
        return
    except PhoneRequiredError as e:
        await views.alert(call, views.err_text(lang, e))
        await _go(call, state, sm, user, lang, "contact")
        return
    except InsufficientBonusError as e:
        await views.alert(call, views.err_text(lang, e))
        await state.update_data(use_bonus=False)
        await _draw(call, state, sm, user, lang, "confirm")
        return
    except ConsentRequiredError:
        await state.clear()
        await screen.safe_answer(call)
        await views.send_onboarding(call.bot, call.from_user.id, user, lang, None)  # type: ignore[arg-type]
        return
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return

    await state.clear()
    b = res.booking
    title = t(lang, "book.confirmed") if res.auto_confirmed else t(lang, "book.sent")
    text = views.booking_text(b, lang, staff=False, title=title + "\n")
    if res.auto_confirmed:
        async with read_tx(sm) as s:
            shop = await settings.get_shop_settings(s)
        if shop.address:
            text += "\n\n" + t(lang, "notify.address", address=h(shop.address))
    elif res.client_soft_blocked:
        text += "\n\n" + t(lang, "book.soft_block_note")
    await screen.render(call, None, text, ckb.result_kb(b, lang))
    await notifier.booking_created(res)
