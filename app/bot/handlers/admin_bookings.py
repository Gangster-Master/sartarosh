"""Admin → 📋 Navbatlar: filters by day / barber / status, phone-or-name search,
pages of 10, booking cards (cancel with reason via the shared card) [#76]."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb, BookingCb, PageCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_date, fmt_day_btn, fmt_dt_short, h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, chunk, kb, noop_btn
from app.bot.states import AdminBookingsFlow
from app.db.enums import BookingStatus
from app.db.uow import read_tx
from app.services import barbers, bookings, clock
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_bookings")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "bk"
PAGE_KEY = "adbk"
FKEY = "adbk_filters"


async def _filters(state: FSMContext) -> dict[str, Any]:
    return dict((await state.get_data()).get(FKEY) or {})


async def show_list(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, *, page: int = 0
) -> None:
    f = await _filters(state)
    day = date.fromisoformat(f["day"]) if f.get("day") else None
    status = BookingStatus(f["status"]) if f.get("status") else None
    try:
        async with read_tx(sm) as s:
            res = await bookings.list_bookings(
                s,
                actor_user_id=user.id,
                day=day,
                barber_id=f.get("barber"),
                status=status,
                phone_or_name=f.get("q"),
                page=page,
                page_size=10,
            )
            barber_name = None
            if f.get("barber"):
                barber_name = (await barbers.get_barber(s, int(f["barber"]))).display_name
    except ServiceError as e:
        await _reply(target, lang, views.err_text(lang, e))
        return
    parts = []
    if day:
        parts.append(fmt_date(day, lang))
    if barber_name:
        parts.append(f"💈 {h(barber_name)}")
    if status:
        parts.append(t(lang, f"status.{status.value}"))
    if f.get("q"):
        parts.append(f"🔎 {h(f['q'])}")
    lines = [t(lang, "admin.bk.title", total=res.total)]
    lines.append(
        t(
            lang,
            "admin.bk.filters",
            filters=" · ".join(parts) if parts else t(lang, "admin.bk.no_filters"),
        )
    )
    lines.append("")
    rows = []
    for b in res.items:
        icon = views.STATUS_ICON.get(b.status, "•")
        phone = f" · {h(b.client_phone)}" if b.client_phone else ""
        lines.append(
            f"{icon} #{b.id} {fmt_dt_short(b.start_at, lang)} · {h(b.barber_name)} · {h(b.client_name)}{phone}"
        )
        rows.append(
            [
                btn(
                    f"{icon} #{b.id} {fmt_dt_short(b.start_at, lang)} {b.client_name[:16]}",
                    BookingCb(a="card", id=b.id),
                )
            ]
        )
    if not res.items:
        lines.append(t(lang, "admin.bk.empty"))
    if res.pages > 1:
        nav = []
        nav.append(btn("◀️", PageCb(k=PAGE_KEY, p=res.page - 1)) if res.page > 0 else noop_btn(" "))
        nav.append(noop_btn(f"{res.page + 1}/{res.pages}"))
        nav.append(
            btn("▶️", PageCb(k=PAGE_KEY, p=res.page + 1))
            if res.page + 1 < res.pages
            else noop_btn(" ")
        )
        rows.append(nav)
    rows.append(
        [
            btn(t(lang, "admin.bk.f_day"), acb(S, "fday")),
            btn(t(lang, "admin.bk.f_barber"), acb(S, "fbar")),
            btn(t(lang, "admin.bk.f_status"), acb(S, "fst")),
        ]
    )
    rows.append(
        [
            btn(t(lang, "admin.bk.search"), acb(S, "q")),
            btn(t(lang, "admin.bk.clear"), acb(S, "clr")),
        ]
    )
    await screen.render(target, None, "\n".join(lines), kb(rows))


async def _reply(target: Target, lang: str, text: str) -> None:
    if isinstance(target, CallbackQuery):
        await views.alert(target, text)
    else:
        await target.answer(text)


async def _set_filter(state: FSMContext, **kw: Any) -> None:
    f = await _filters(state)
    for k, v in kw.items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = v
    await state.set_state(None)
    await state.update_data({FKEY: f})


@router.callback_query(PageCb.filter(F.k == PAGE_KEY), ANY)
async def on_page(
    call: CallbackQuery, callback_data: PageCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    await show_list(call, state, sm, user, lang, page=max(0, callback_data.p))


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_bookings(
    call: CallbackQuery, callback_data: AdminCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    a, v, bid = callback_data.a, callback_data.v, callback_data.id
    if a == "list":
        await state.set_state(None)
        await show_list(call, state, sm, user, lang)
    elif a == "clr":
        await state.clear()
        await show_list(call, state, sm, user, lang)
    elif a == "fday":
        today = clock.local_today()
        days = [today + timedelta(days=i) for i in range(-1, 8)]
        buttons = [
            btn(
                fmt_day_btn(d, today, lang) if d >= today else fmt_date(d, lang),
                acb(S, "setd", 0, d.strftime("%Y%m%d")),
            )
            for d in days
        ]
        rows = chunk(buttons, 3)
        rows.append([btn(t(lang, "admin.bk.all_days"), acb(S, "setd", 0, "all"))])
        rows.append([back_btn(lang, S)])
        await screen.render(call, None, t(lang, "admin.bk.pick_day"), kb(rows))
    elif a == "setd":
        day = views.parse_day(v)
        await _set_filter(state, day=day.isoformat() if day else None)
        await show_list(call, state, sm, user, lang)
    elif a == "fbar":
        async with read_tx(sm) as s:
            items = await barbers.list_barbers(s, public_only=False)
        rows = [[btn(f"💈 {b.display_name}", acb(S, "setb", b.id))] for b in items]
        rows.append([btn(t(lang, "admin.bk.all_barbers"), acb(S, "setb", 0))])
        rows.append([back_btn(lang, S)])
        await screen.render(call, None, t(lang, "admin.bk.pick_barber"), kb(rows))
    elif a == "setb":
        await _set_filter(state, barber=bid or None)
        await show_list(call, state, sm, user, lang)
    elif a == "fst":
        buttons = [
            btn(
                f"{views.STATUS_ICON.get(st, '')} {t(lang, f'status.{st.value}')}",
                acb(S, "sets", 0, st.value),
            )
            for st in BookingStatus
        ]
        rows = chunk(buttons, 2)
        rows.append([btn(t(lang, "admin.bk.all_statuses"), acb(S, "sets", 0, "all"))])
        rows.append([back_btn(lang, S)])
        await screen.render(call, None, t(lang, "admin.bk.pick_status"), kb(rows))
    elif a == "sets":
        valid = {st.value for st in BookingStatus}
        await _set_filter(state, status=v if v in valid else None)
        await show_list(call, state, sm, user, lang)
    elif a == "q":
        await state.set_state(AdminBookingsFlow.search)
        await screen.render(call, state, t(lang, "admin.bk.ask_search"), input_cancel_kb(lang))
    else:
        await views.stale(call, lang)


@router.message(StateFilter(AdminBookingsFlow.search), F.text)
async def on_search(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    q = " ".join((message.text or "").split())[:64]
    if len(q) < 2:
        await message.answer(t(lang, "admin.bk.ask_search"))
        return
    await _set_filter(state, q=q)
    await show_list(message, state, sm, user, lang)


@router.message(StateFilter(AdminBookingsFlow.search))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
