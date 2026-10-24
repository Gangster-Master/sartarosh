"""Admin → 📊 Statistika: today / 7 days / this month / custom range, per barber,
top services, new clients, and the manual-completion share [D2][C6]."""

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
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_date_short, fmt_price, h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, kb
from app.bot.states import AdminStatsFlow
from app.db.uow import read_tx
from app.services import clock, stats
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_stats")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "stat"


def _menu_kb(lang: str) -> Any:
    return kb(
        [
            [
                btn(t(lang, "admin.stat.today"), acb(S, "r", 0, "today")),
                btn(t(lang, "admin.stat.week"), acb(S, "r", 0, "7d")),
            ],
            [
                btn(t(lang, "admin.stat.month"), acb(S, "r", 0, "month")),
                btn(t(lang, "admin.stat.custom"), acb(S, "custom")),
            ],
        ]
    )


async def show_menu(target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str) -> None:
    await screen.render(target, None, t(lang, "admin.stat.title"), _menu_kb(lang))


def _range(code: str) -> tuple[date, date] | None:
    today = clock.local_today()
    if code == "today":
        return today, today
    if code == "7d":
        return today - timedelta(days=6), today
    if code == "month":
        return today.replace(day=1), today
    return None


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "0%"


async def show_stats(target: Target, sm: SM, user: Any, lang: str, start: date, end: date) -> None:
    try:
        async with read_tx(sm) as s:
            st = await stats.get_stats(s, actor_user_id=user.id, start=start, end=end)
    except ServiceError as e:
        if isinstance(target, CallbackQuery):
            await views.alert(target, views.err_text(lang, e))
        else:
            await target.answer(views.err_text(lang, e))
        return
    period = (
        fmt_date_short(start) if start == end else f"{fmt_date_short(start)}–{fmt_date_short(end)}"
    )
    lines = [t(lang, "admin.stat.header", period=period), ""]
    tot = st.totals
    lines.append(
        t(
            lang,
            "admin.stat.totals",
            total=tot.total,
            completed=tot.completed,
            no_show=tot.no_show,
            cancelled=tot.cancelled,
            rejected=tot.rejected,
            expired=tot.expired,
        )
    )
    lines.append(t(lang, "admin.stat.revenue", amount=fmt_price(tot.revenue_uzs, lang)))
    lines.append(
        t(
            lang,
            "admin.stat.manual_share",
            manual=tot.manual_completed,
            share=_pct(tot.manual_completed, tot.completed),
            auto=tot.auto_completed,
        )
    )
    lines.append(t(lang, "admin.stat.new_clients", n=st.new_clients))
    if st.rows:
        lines.extend(["", t(lang, "admin.stat.by_barber")])
        for r in st.rows:
            lines.append(
                t(
                    lang,
                    "admin.stat.barber_row",
                    name=h(r.barber_name),
                    completed=r.completed,
                    total=r.total,
                    no_show=r.no_show,
                    manual=_pct(r.manual_completed, r.completed),
                    amount=fmt_price(r.revenue_uzs, lang),
                )
            )
    if st.top_services:
        lines.extend(["", t(lang, "admin.stat.top_services")])
        for x in st.top_services[:5]:
            lines.append(f"• {h(x.service_name)}: {x.completed} · {fmt_price(x.revenue_uzs, lang)}")
    await screen.render(target, None, "\n".join(lines), kb([[back_btn(lang, S, "menu")]]))


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_stats(
    call: CallbackQuery, callback_data: AdminCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    a = callback_data.a
    if a == "menu":
        await state.clear()
        await show_menu(call, state, sm, user, lang)
    elif a == "r":
        rng = _range(callback_data.v)
        if rng is None:
            await views.stale(call, lang)
            return
        await show_stats(call, sm, user, lang, *rng)
    elif a == "custom":
        await state.clear()
        await state.set_state(AdminStatsFlow.range)
        await screen.render(call, state, t(lang, "admin.stat.ask_range"), input_cancel_kb(lang))
    else:
        await views.stale(call, lang)


@router.message(StateFilter(AdminStatsFlow.range), F.text)
async def on_range(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    raw = (message.text or "").replace("–", "-").replace("—", "-").replace(" ", "")
    today = clock.local_today()
    parts = raw.split("-") if raw.count("-") == 1 else [raw, raw]
    start = views.parse_date_text(parts[0], date(today.year, 1, 1))
    end = views.parse_date_text(parts[1], date(today.year, 1, 1))
    if start is None or end is None or start > end or (end - start).days > 366 or start > today:
        await message.answer(t(lang, "admin.stat.ask_range"))
        return
    await state.clear()
    await show_stats(message, sm, user, lang, start, end)


@router.message(StateFilter(AdminStatsFlow.range))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
