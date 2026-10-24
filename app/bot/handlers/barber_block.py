"""Barber time blocking: "⏸ Hozir bandman" [F1] and BlockTimeFlow [#27][#31]."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import BlockCb, BusyCb
from app.bot.filters import IsBarber
from app.bot.i18n import fmt_date, fmt_day_btn, fmt_dt, fmt_dt_short, fmt_time, h, t
from app.bot.keyboards import barber as bkb
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.notifier import Notifier
from app.bot.states import BlockTimeFlow
from app.db.uow import read_tx, write_tx
from app.services import clock, schedule, settings, validation
from app.services.errors import InputError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="barber_block")
router.message.filter(F.chat.type == "private")
router.message.filter(IsBarber())
router.callback_query.filter(IsBarber())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message

BLOCK_DAYS = 14
GRID_MIN = 30


# ------------------------------------------------------------------ busy now [F1]


async def _blocking_allowed(sm: SM, roles: Any) -> bool:
    """Early check of the barbers_can_block_time setting (the service checks it again)."""
    if getattr(roles, "is_admin", False):
        return True
    async with read_tx(sm) as s:
        return (await settings.get_shop_settings(s)).barbers_can_block_time


async def show_busy(message: Message, lang: str, sm: SM | None = None, roles: Any = None) -> None:
    if sm is not None and not await _blocking_allowed(sm, roles):
        await message.answer(t(lang, "err.forbidden.setting"))
        return
    await message.answer(t(lang, "barber.busy_prompt"), reply_markup=bkb.busy_kb(lang))


@router.callback_query(BusyCb.filter(), ANY)
async def on_busy(call: CallbackQuery, callback_data: BusyCb, sm: SM, user: Any, lang: str) -> None:
    if callback_data.m not in (30, 60):
        await views.stale(call, lang)
        return
    try:
        async with write_tx(sm) as s:
            res = await schedule.busy_now(s, actor_user_id=user.id, minutes=callback_data.m)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    lines = [
        t(
            lang,
            "barber.busy_done",
            start=fmt_time(res.time_off.start_at),
            end=fmt_time(res.time_off.end_at),
        )
    ]
    for b in res.overlapping_kept:
        lines.append(
            t(lang, "barber.busy_overlap", time=fmt_time(b.start_at), name=h(b.client_name))
        )
    await screen.render(call, None, "\n".join(lines))


# ------------------------------------------------------------------ block flow


def _menu_kb(lang: str) -> Any:
    today = clock.local_today()
    days = [today + timedelta(days=i) for i in range(BLOCK_DAYS)]
    buttons = [
        btn(fmt_day_btn(d, today, lang), BlockCb(a="day", d=d.strftime("%Y%m%d"))) for d in days
    ]
    rows = chunk(buttons, 3)
    rows.append([btn(t(lang, "barber.block_list"), BlockCb(a="list"))])
    rows.append([btn(t(lang, "common.cancel"), BlockCb(a="abort"))])
    return kb(rows)


async def start_block(target: Target, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    await state.clear()
    if not await _blocking_allowed(sm, roles):
        if isinstance(target, CallbackQuery):
            await views.alert(target, t(lang, "err.forbidden.setting"))
        else:
            await target.answer(t(lang, "err.forbidden.setting"))
        return
    await state.set_state(BlockTimeFlow.day)
    await screen.render(target, state, t(lang, "barber.block_pick_day"), _menu_kb(lang))


async def _day_window(sm: SM, barber_id: int, day: date) -> tuple[time, time]:
    async with read_tx(sm) as s:
        week = await schedule.get_week_hours(s, barber_id)
    for wh in week:
        if wh.weekday == day.weekday():
            return wh.start_time, wh.end_time
    return time(8, 0), time(22, 0)


def _grid(
    day: date, start: time, end: time, *, after: datetime | None = None, include_end: bool = False
) -> list[datetime]:
    out = []
    cur = clock.local_dt(day, start)
    last = clock.local_dt(day, end)
    while cur < last or (include_end and cur == last):
        if after is None or cur > after:
            out.append(cur)
        cur += timedelta(minutes=GRID_MIN)
    return out


async def _check_state(
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


@router.callback_query(BlockCb.filter(F.a == "list"), ANY)
async def on_list(call: CallbackQuery, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    now = clock.now_utc()
    async with read_tx(sm) as s:
        items = await schedule.list_time_off(
            s, barber_id=roles.barber_id, start=now, end=now + timedelta(days=60)
        )
    lines = [t(lang, "barber.block_list_title")]
    rows = []
    own = [x for x in items if x.barber_id == roles.barber_id]
    if not own:
        lines.append(t(lang, "barber.block_list_empty"))
    for x in own:
        reason = f" · {h(x.reason)}" if x.reason else ""
        lines.append(f"• {fmt_dt_short(x.start_at, lang)} – {fmt_dt_short(x.end_at, lang)}{reason}")
        rows.append([btn(f"🗑 {fmt_dt_short(x.start_at, lang)}", BlockCb(a="del", t=str(x.id)))])
    rows.append([btn(t(lang, "common.back"), BlockCb(a="menu"))])
    await state.set_state(BlockTimeFlow.day)
    await screen.render(call, state, "\n".join(lines), kb(rows))


@router.callback_query(BlockCb.filter(F.a == "del"), ANY)
async def on_delete(
    call: CallbackQuery,
    callback_data: BlockCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    if not callback_data.t.isdigit():
        await views.stale(call, lang)
        return
    try:
        async with write_tx(sm) as s:
            await schedule.delete_time_off(
                s, actor_user_id=user.id, time_off_id=int(callback_data.t)
            )
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    await screen.safe_answer(call, t(lang, "barber.block_deleted"))
    await on_list(call, state, sm, roles, lang)


@router.callback_query(BlockCb.filter(F.a.in_(("menu", "abort"))), ANY)
async def on_menu(
    call: CallbackQuery, callback_data: BlockCb, state: FSMContext, sm: SM, roles: Any, lang: str
) -> None:
    if callback_data.a == "abort":
        await state.clear()
        await screen.render(call, None, t(lang, "common.cancelled"))
        return
    await start_block(call, state, sm, roles, lang)


@router.callback_query(BlockCb.filter(F.a == "day"), ANY)
async def on_day(
    call: CallbackQuery, callback_data: BlockCb, state: FSMContext, sm: SM, roles: Any, lang: str
) -> None:
    data = await _check_state(
        call, state, lang, BlockTimeFlow.day, BlockTimeFlow.start, BlockTimeFlow.end
    )
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    today = clock.local_today()
    if day is None or day < today or day > today + timedelta(days=60):
        await views.stale(call, lang)
        return
    ws, we = await _day_window(sm, roles.barber_id, day)
    now = clock.now_utc()
    starts = _grid(day, ws, we, after=now - timedelta(minutes=GRID_MIN))
    buttons = [
        btn(
            fmt_time(x), BlockCb(a="start", d=callback_data.d, t=clock.to_local(x).strftime("%H%M"))
        )
        for x in starts
    ]
    rows = [[btn(t(lang, "barber.block_whole"), BlockCb(a="whole", d=callback_data.d))]]
    rows.extend(chunk(buttons, 4))
    rows.append(
        [
            btn(t(lang, "common.back"), BlockCb(a="menu")),
            btn(t(lang, "common.cancel"), BlockCb(a="abort")),
        ]
    )
    await state.set_state(BlockTimeFlow.start)
    await state.update_data(day=callback_data.d)
    await screen.render(
        call, state, t(lang, "barber.block_pick_start", day=fmt_date(day, lang)), kb(rows)
    )


@router.callback_query(BlockCb.filter(F.a == "start"), ANY)
async def on_start(
    call: CallbackQuery, callback_data: BlockCb, state: FSMContext, sm: SM, roles: Any, lang: str
) -> None:
    data = await _check_state(call, state, lang, BlockTimeFlow.start)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    tm = views.parse_hhmm(callback_data.t)
    if day is None or tm is None or data.get("day") != callback_data.d:
        await views.stale(call, lang)
        return
    start = clock.local_dt(day, tm)
    ws, we = await _day_window(sm, roles.barber_id, day)
    ends = _grid(day, ws, we, after=start, include_end=True)
    if not ends:
        ends = [start + timedelta(minutes=GRID_MIN)]
    buttons = [
        btn(fmt_time(x), BlockCb(a="end", d=callback_data.d, t=clock.to_local(x).strftime("%H%M")))
        for x in ends
    ]
    rows = chunk(buttons, 4)
    rows.append(
        [
            btn(t(lang, "common.back"), BlockCb(a="day", d=callback_data.d)),
            btn(t(lang, "common.cancel"), BlockCb(a="abort")),
        ]
    )
    await state.set_state(BlockTimeFlow.end)
    await state.update_data(start_at=start.isoformat())
    await screen.render(
        call, state, t(lang, "barber.block_pick_end", start=fmt_dt(start, lang)), kb(rows)
    )


async def _ask_reason(
    call: CallbackQuery, state: FSMContext, lang: str, start: datetime, end: datetime
) -> None:
    await state.set_state(BlockTimeFlow.reason)
    await state.update_data(start_at=start.isoformat(), end_at=end.isoformat())
    text = t(lang, "barber.block_reason", start=fmt_dt(start, lang), end=fmt_dt(end, lang))
    rows = [
        [btn(t(lang, "barber.block_no_reason"), BlockCb(a="noreason"))],
        [btn(t(lang, "common.cancel"), BlockCb(a="abort"))],
    ]
    await screen.render(call, state, text, kb(rows))


@router.callback_query(BlockCb.filter(F.a == "whole"), ANY)
async def on_whole(
    call: CallbackQuery, callback_data: BlockCb, state: FSMContext, lang: str
) -> None:
    data = await _check_state(call, state, lang, BlockTimeFlow.start)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    if day is None or data.get("day") != callback_data.d:
        await views.stale(call, lang)
        return
    iv = schedule.full_day_interval(day)
    await _ask_reason(call, state, lang, iv.start, iv.end)


@router.callback_query(BlockCb.filter(F.a == "end"), ANY)
async def on_end(call: CallbackQuery, callback_data: BlockCb, state: FSMContext, lang: str) -> None:
    data = await _check_state(call, state, lang, BlockTimeFlow.end)
    if data is None:
        return
    day = views.parse_day(callback_data.d)
    tm = views.parse_hhmm(callback_data.t)
    if day is None or tm is None or not data.get("start_at"):
        await views.stale(call, lang)
        return
    start = datetime.fromisoformat(data["start_at"])
    end = clock.local_dt(day, tm)
    if end <= start:
        await views.stale(call, lang)
        return
    await _ask_reason(call, state, lang, start, end)


async def _preview_and_save(
    target: Target,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
    reason: str | None,
) -> None:
    data = await state.get_data()
    start = datetime.fromisoformat(data["start_at"])
    end = datetime.fromisoformat(data["end_at"])
    await state.update_data(reason=reason)
    try:
        async with read_tx(sm) as s:
            conflicts = await schedule.preview_time_off_conflicts(
                s, actor_user_id=user.id, barber_id=roles.barber_id, start_at=start, end_at=end
            )
    except ServiceError as e:
        await state.clear()
        await screen.render(target, None, views.err_text(lang, e))
        return
    if not conflicts:
        await _save(target, state, sm, notifier, user, roles, lang, cancel_conflicts=False)
        return
    lines = [t(lang, "barber.block_conflicts", n=len(conflicts))]
    for b in conflicts:
        phone = f" · {h(b.client_phone)}" if b.client_phone else ""
        lines.append(f"• {fmt_dt_short(b.start_at, lang)} {h(b.client_name)}{phone}")
    rows = [
        [btn(t(lang, "barber.block_cancel_notify"), BlockCb(a="cancelcf"))],
        [btn(t(lang, "barber.block_keep"), BlockCb(a="keep"))],
        [btn(t(lang, "common.cancel"), BlockCb(a="abort"))],
    ]
    await state.set_state(BlockTimeFlow.confirm_conflicts)
    await screen.render(target, state, "\n".join(lines), kb(rows))


async def _save(
    target: Target,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
    *,
    cancel_conflicts: bool,
) -> None:
    data = await state.get_data()
    start = datetime.fromisoformat(data["start_at"])
    end = datetime.fromisoformat(data["end_at"])
    try:
        async with write_tx(sm) as s:
            res = await schedule.add_time_off(
                s,
                actor_user_id=user.id,
                barber_id=roles.barber_id,
                start_at=start,
                end_at=end,
                reason=data.get("reason"),
                cancel_conflicts=cancel_conflicts,
            )
    except ServiceError as e:
        await state.clear()
        await screen.render(target, None, views.err_text(lang, e))
        return
    await state.clear()
    lines = [
        t(
            lang,
            "barber.block_saved",
            start=fmt_dt(res.time_off.start_at, lang),
            end=fmt_dt(res.time_off.end_at, lang),
        )
    ]
    if res.cancelled:
        lines.append(t(lang, "barber.block_cancelled_n", n=len(res.cancelled)))
    if res.overlapping_kept:
        lines.append(t(lang, "barber.block_kept_n", n=len(res.overlapping_kept)))
    await screen.render(
        target, None, "\n".join(lines), kb([[btn(t(lang, "barber.block_more"), BlockCb(a="menu"))]])
    )
    if res.cancelled:
        await notifier.time_off_cancellations(res.cancelled)


@router.callback_query(BlockCb.filter(F.a == "noreason"), ANY)
async def on_no_reason(
    call: CallbackQuery,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _check_state(call, state, lang, BlockTimeFlow.reason)
    if data is None:
        return
    await _preview_and_save(call, state, sm, notifier, user, roles, lang, None)


@router.message(StateFilter(BlockTimeFlow.reason), F.text)
async def on_reason_text(
    message: Message,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    try:
        reason = validation.validate_comment(message.text or "")
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await _preview_and_save(
        message, state, sm, notifier, user, roles, lang, (reason or "")[:200] or None
    )


@router.callback_query(BlockCb.filter(F.a.in_(("cancelcf", "keep"))), ANY)
async def on_conflict_choice(
    call: CallbackQuery,
    callback_data: BlockCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    data = await _check_state(call, state, lang, BlockTimeFlow.confirm_conflicts)
    if data is None:
        return
    await _save(
        call, state, sm, notifier, user, roles, lang, cancel_conflicts=callback_data.a == "cancelcf"
    )
