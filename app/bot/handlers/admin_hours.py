"""Admin → 🕒 Ish vaqti: per barber and weekday, with a break; "copy to all days";
shrinking the hours lists the affected bookings without cancelling them."""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_dt_short, h, t, weekday_name, weekday_short
from app.bot.keyboards.admin import acb, back_btn
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.states import AdminHoursFlow
from app.db.uow import read_tx, write_tx
from app.services import barbers, schedule
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_hours")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "wh"
# one-tap ranges for a working day (callback values carry no ':' — that is the separator)
RANGE_PRESETS = ("09:00-18:00", "09:00-20:00", "10:00-19:00", "10:00-20:00", "10:00-21:00")
STD_HOURS = (time(10), time(20), time(13), time(14))  # Mon–Sat preset for a barber with no hours


def _pack(raw: str) -> str:
    return raw.replace(":", "")


def _unpack_range(v: str) -> tuple[time, time] | None:
    a, _, b = v.partition("-")
    return (
        views.parse_time_range(f"{a[:2]}:{a[2:]}-{b[:2]}:{b[2:]}")
        if len(a) == len(b) == 4
        else None
    )


def _fmt(tm: time | None) -> str:
    return tm.strftime("%H:%M") if tm else "—"


def _day_text(wh: Any | None, lang: str) -> str:
    if wh is None:
        return t(lang, "admin.wh.off")
    line = f"{_fmt(wh.start_time)}–{_fmt(wh.end_time)}"
    if wh.break_start and wh.break_end:
        line += " " + t(lang, "admin.wh.break", start=_fmt(wh.break_start), end=_fmt(wh.break_end))
    return line


async def show_pick_barber(
    target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str
) -> None:
    async with read_tx(sm) as s:
        items = [b for b in await barbers.list_barbers(s, public_only=False) if b.is_active]
    rows = [[btn(f"💈 {b.display_name}", acb(S, "pick", b.id))] for b in items]
    text = t(lang, "admin.wh.pick_barber") if items else t(lang, "admin.br.empty")
    await screen.render(target, None, text, kb(rows))


async def show_week(
    target: Target, sm: SM, lang: str, barber_id: int, *, note: str | None = None
) -> None:
    async with read_tx(sm) as s:
        b = await barbers.get_barber(s, barber_id)
        week = {w.weekday: w for w in await schedule.get_week_hours(s, barber_id)}
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(t(lang, "admin.wh.title", name=h(b.display_name)))
    lines.append("")
    for wd in range(7):
        lines.append(f"<b>{weekday_short(wd, lang)}</b>  {_day_text(week.get(wd), lang)}")
    lines.extend(["", t(lang, "admin.wh.no_hours") if not week else t(lang, "admin.wh.tap_day")])
    buttons = [btn(weekday_short(wd, lang), acb(S, "wd", barber_id, str(wd))) for wd in range(7)]
    rows = []
    if not week:  # a fresh barber: one tap to a usable schedule
        rows.append([btn(t(lang, "admin.wh.std"), acb(S, "std", barber_id))])
        rows.append([btn(t(lang, "admin.wh.copy_barber"), acb(S, "cpb", barber_id))])
    rows.extend(chunk(buttons, 4))
    if week:
        rows.append([btn(t(lang, "admin.wh.copy"), acb(S, "copy", barber_id))])
    rows.append([back_btn(lang, S, "barbers")])
    await screen.render(target, None, "\n".join(lines), kb(rows))


def _out_of_hours_note(items: list[Any], lang: str) -> str:
    if not items:
        return t(lang, "admin.saved")
    lines = [t(lang, "admin.wh.out_of_hours", n=len(items))]
    for b in items[:20]:
        lines.append(f"• {fmt_dt_short(b.start_at, lang)} {h(b.client_name)}")
    return "\n".join(lines)


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_hours(
    call: CallbackQuery, callback_data: AdminCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    a, bid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "barbers":
            await state.clear()
            await show_pick_barber(call, state, sm, user, lang)
        elif a == "pick":
            await state.clear()
            await show_week(call, sm, lang, bid)
        elif a == "wd":
            if not v.isdigit() or not 0 <= int(v) <= 6:
                await views.stale(call, lang)
                return
            wd = int(v)
            await state.clear()
            await state.set_state(AdminHoursFlow.start)
            await state.update_data(barber_id=bid, weekday=wd)
            presets = [
                btn(raw.replace("-", "–"), acb(S, "rng", bid, f"{wd}|{_pack(raw)}"))
                for raw in RANGE_PRESETS
            ]
            rows = chunk(presets, 2)
            rows.append([btn(t(lang, "admin.wh.set_off"), acb(S, "off", bid, str(wd)))])
            rows.append([btn(t(lang, "common.cancel"), acb(S, "pick", bid))])
            text = t(lang, "admin.wh.ask_range", day=weekday_name(wd, lang).capitalize())
            await screen.render(call, state, text, kb(rows))
        elif a == "off":
            if not v.isdigit():
                await views.stale(call, lang)
                return
            async with write_tx(sm) as s:
                res = await schedule.set_working_hours(
                    s,
                    actor_user_id=user.id,
                    barber_id=bid,
                    weekday=int(v),
                    start_time=None,
                    end_time=None,
                )
            await state.clear()
            await show_week(call, sm, lang, bid, note=_out_of_hours_note(res.out_of_hours, lang))
        elif a == "rng":
            wd_s, _, packed = v.partition("|")
            rng = _unpack_range(packed)
            data = await state.get_data()
            if (
                rng is None
                or await state.get_state() != AdminHoursFlow.start.state
                or data.get("barber_id") != bid
                or str(data.get("weekday")) != wd_s
            ):
                await views.stale(call, lang)
                return
            await _ask_break(call, state, lang, bid, rng[0], rng[1])
        elif a in ("nobr", "brk"):
            if await state.get_state() != AdminHoursFlow.break_.state:
                await views.stale(call, lang)
                return
            rng = _unpack_range(v) if a == "brk" else None
            if a == "brk" and rng is None:
                await views.stale(call, lang)
                return
            await _save(call, state, sm, user, lang, *(rng or (None, None)))
        elif a == "std":
            async with write_tx(sm) as s:
                if await schedule.get_week_hours(s, bid):  # stale button: never overwrite
                    raise _AlreadySet
                st, en, bs, be = STD_HOURS
                for wd in range(6):
                    await schedule.set_working_hours(
                        s,
                        actor_user_id=user.id,
                        barber_id=bid,
                        weekday=wd,
                        start_time=st,
                        end_time=en,
                        break_start=bs,
                        break_end=be,
                    )
            await state.clear()
            await show_week(call, sm, lang, bid, note=t(lang, "admin.saved"))
        elif a == "cpb":
            async with read_tx(sm) as s:
                others = [
                    (b, await schedule.get_week_hours(s, b.id))
                    for b in await barbers.list_barbers(s, public_only=False)
                    if b.is_active and b.id != bid
                ]
            rows = [
                [btn(f"💈 {b.display_name}", acb(S, "cpbg", bid, str(b.id)))]
                for b, wk in others
                if wk
            ]
            if not rows:
                await views.alert(call, t(lang, "admin.wh.copy_barber_none"))
                return
            rows.append([back_btn(lang, S, "pick", bid)])
            await screen.render(call, None, t(lang, "admin.wh.copy_barber_pick"), kb(rows))
        elif a == "cpbg":
            if not v.isdigit():
                await views.stale(call, lang)
                return
            async with write_tx(sm) as s:
                if await schedule.get_week_hours(s, bid):
                    raise _AlreadySet
                for w in await schedule.get_week_hours(s, int(v)):
                    await schedule.set_working_hours(
                        s,
                        actor_user_id=user.id,
                        barber_id=bid,
                        weekday=w.weekday,
                        start_time=w.start_time,
                        end_time=w.end_time,
                        break_start=w.break_start,
                        break_end=w.break_end,
                    )
            await state.clear()
            await show_week(call, sm, lang, bid, note=t(lang, "admin.saved"))
        elif a == "copy":
            async with read_tx(sm) as s:
                week = await schedule.get_week_hours(s, bid)
            if not week:
                await views.alert(call, t(lang, "admin.wh.copy_none"))
                return
            buttons = [
                btn(
                    f"{weekday_short(w.weekday, lang)} {_fmt(w.start_time)}–{_fmt(w.end_time)}",
                    acb(S, "copyfrom", bid, str(w.weekday)),
                )
                for w in week
            ]
            rows = chunk(buttons, 2)
            rows.append([back_btn(lang, S, "pick", bid)])
            await screen.render(call, None, t(lang, "admin.wh.copy_from"), kb(rows))
        elif a == "copyfrom":
            rows = [
                [btn(t(lang, "admin.wh.copy_mon_sat"), acb(S, "cp", bid, f"{v}-0"))],
                [btn(t(lang, "admin.wh.copy_all"), acb(S, "cp", bid, f"{v}-1"))],
                [back_btn(lang, S, "copy", bid)],
            ]
            await screen.render(call, None, t(lang, "admin.wh.copy_scope"), kb(rows))
        elif a == "cp":
            src, _, sun = v.partition("-")
            if not src.isdigit():
                await views.stale(call, lang)
                return
            async with write_tx(sm) as s:
                out = await schedule.copy_hours_to_all_weekdays(
                    s,
                    actor_user_id=user.id,
                    barber_id=bid,
                    source_weekday=int(src),
                    include_sunday=sun == "1",
                )
            await show_week(call, sm, lang, bid, note=_out_of_hours_note(out, lang))
        else:
            await views.stale(call, lang)
    except _AlreadySet:
        await state.clear()
        await show_week(call, sm, lang, bid)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


class _AlreadySet(Exception):
    """A one-tap preset arrived after the barber already got hours."""


async def _ask_break(
    target: Target, state: FSMContext, lang: str, bid: int, start: time, end: time
) -> None:
    await state.update_data(start=start.strftime("%H:%M"), end=end.strftime("%H:%M"))
    await state.set_state(AdminHoursFlow.break_)
    rows = [
        [
            btn("☕ 13:00–14:00", acb(S, "brk", bid, "1300-1400")),
            btn(t(lang, "admin.wh.no_break"), acb(S, "nobr", bid)),
        ],
        [btn(t(lang, "common.cancel"), acb(S, "pick", bid))],
    ]
    await screen.render(target, state, t(lang, "admin.wh.ask_break"), kb(rows))


@router.message(StateFilter(AdminHoursFlow.start), F.text)
async def on_range(message: Message, state: FSMContext, lang: str) -> None:
    rng = views.parse_time_range(message.text or "")
    if rng is None:
        await message.answer(t(lang, "admin.wh.bad_range"))
        return
    data = await state.get_data()
    await _ask_break(message, state, lang, int(data["barber_id"]), rng[0], rng[1])


@router.message(StateFilter(AdminHoursFlow.break_), F.text)
async def on_break(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    rng = views.parse_time_range(message.text or "")
    if rng is None:
        await message.answer(t(lang, "admin.wh.bad_range"))
        return
    await _save(message, state, sm, user, lang, rng[0], rng[1])


async def _save(
    target: Target,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    br_start: time | None,
    br_end: time | None,
) -> None:
    data = await state.get_data()
    bid, wd = int(data["barber_id"]), int(data["weekday"])
    start = views.parse_time_text(data["start"])
    end = views.parse_time_text(data["end"])
    try:
        async with write_tx(sm) as s:
            res = await schedule.set_working_hours(
                s,
                actor_user_id=user.id,
                barber_id=bid,
                weekday=wd,
                start_time=start,
                end_time=end,
                break_start=br_start,
                break_end=br_end,
            )
    except ServiceError as e:
        msg = views.err_text(lang, e)
        if isinstance(target, CallbackQuery):
            await views.alert(target, msg)
        else:
            await target.answer(msg)
        return
    await state.clear()
    await show_week(target, sm, lang, bid, note=_out_of_hours_note(res.out_of_hours, lang))


@router.message(StateFilter(AdminHoursFlow))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
