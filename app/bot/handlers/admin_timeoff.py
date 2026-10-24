"""Admin → 🌴 Dam olish kunlari: per barber or shop-wide, single day / range /
hours, with the conflict flow (cancel + notify, or keep) [#27][#31]."""

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
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_date, fmt_day_btn, fmt_dt, fmt_dt_short, h, t
from app.bot.keyboards.admin import acb, back_btn
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.notifier import Notifier
from app.bot.states import AdminTimeOffFlow
from app.db.uow import read_tx, write_tx
from app.services import barbers, clock, schedule, validation
from app.services.errors import InputError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_timeoff")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "to"
PICK_DAYS = 21


async def show_targets(
    target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str
) -> None:
    async with read_tx(sm) as s:
        items = [b for b in await barbers.list_barbers(s, public_only=False) if b.is_active]
    rows = [[btn(t(lang, "admin.to.shop"), acb(S, "tgt", 0))]]
    rows.extend([btn(f"💈 {b.display_name}", acb(S, "tgt", b.id))] for b in items)
    await screen.render(target, None, t(lang, "admin.to.pick_target"), kb(rows))


async def _target_name(sm: SM, lang: str, target_id: int) -> str:
    if not target_id:
        return t(lang, "admin.to.shop")
    async with read_tx(sm) as s:
        return h((await barbers.get_barber(s, target_id)).display_name)


async def show_target(
    target: Target, sm: SM, lang: str, target_id: int, *, note: str | None = None
) -> None:
    now = clock.now_utc()
    async with read_tx(sm) as s:
        items = await schedule.list_time_off(
            s, barber_id=target_id or None, start=now, end=now + timedelta(days=180)
        )
    own = [x for x in items if (x.barber_id or 0) == target_id]
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(t(lang, "admin.to.title", name=await _target_name(sm, lang, target_id)))
    rows = []
    if not own:
        lines.append(t(lang, "admin.to.empty"))
    for x in own:
        reason = f" · {h(x.reason)}" if x.reason else ""
        lines.append(f"• {fmt_dt_short(x.start_at, lang)} – {fmt_dt_short(x.end_at, lang)}{reason}")
        rows.append(
            [btn(f"🗑 {fmt_dt_short(x.start_at, lang)}", acb(S, "del", x.id, str(target_id)))]
        )
    rows.append([btn(t(lang, "admin.to.new"), acb(S, "new", target_id))])
    rows.append([back_btn(lang, S, "targets")])
    await screen.render(target, None, "\n".join(lines), kb(rows))


def _days_kb(
    lang: str, a: str, target_id: int, first: date, *, extra: list[Any] | None = None
) -> Any:
    today = clock.local_today()
    buttons = [
        btn(
            fmt_day_btn(first + timedelta(days=i), today, lang),
            acb(S, a, target_id, (first + timedelta(days=i)).strftime("%Y%m%d")),
        )
        for i in range(PICK_DAYS if a == "sd" else 14)
    ]
    rows = []
    if extra:
        rows.append(extra)
    rows.extend(chunk(buttons, 3))
    rows.append([btn(t(lang, "common.cancel"), acb(S, "tgt", target_id))])
    return kb(rows)


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_timeoff(
    call: CallbackQuery,
    callback_data: AdminCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    a, tid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "targets":
            await state.clear()
            await show_targets(call, state, sm, user, lang)
        elif a == "tgt":
            await state.clear()
            await show_target(call, sm, lang, tid)
        elif a == "del":
            async with write_tx(sm) as s:
                await schedule.delete_time_off(s, actor_user_id=user.id, time_off_id=tid)
            target_id = int(v) if v.isdigit() else 0
            await show_target(call, sm, lang, target_id, note=t(lang, "admin.to.deleted"))
        elif a == "new":
            await state.clear()
            await state.set_state(AdminTimeOffFlow.start_day)
            await state.update_data(target=tid)
            await screen.render(
                call,
                state,
                t(lang, "admin.to.ask_start"),
                _days_kb(lang, "sd", tid, clock.local_today()),
            )
        elif a == "sd":
            await _pick_start(call, state, lang, views.parse_day(v))
        elif a == "ed":
            await _pick_end(call, state, lang, views.parse_day(v))
        elif a == "whole":
            if await state.get_state() != AdminTimeOffFlow.hours.state:
                await views.stale(call, lang)
                return
            data = await state.get_data()
            day = date.fromisoformat(data["start_day"])
            await _ask_reason(
                call,
                state,
                lang,
                schedule.full_day_interval(day).start,
                schedule.full_day_interval(day).end,
            )
        elif a == "rs":
            if await state.get_state() != AdminTimeOffFlow.reason.state:
                await views.stale(call, lang)
                return
            await _preview(call, state, sm, notifier, user, lang, None)
        elif a == "cf":
            if await state.get_state() != AdminTimeOffFlow.confirm_conflicts.state:
                await views.stale(call, lang)
                return
            await _save(call, state, sm, notifier, user, lang, cancel_conflicts=v == "1")
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


async def _pick_start(target: Target, state: FSMContext, lang: str, day: date | None) -> None:
    if await state.get_state() != AdminTimeOffFlow.start_day.state:
        if isinstance(target, CallbackQuery):
            await views.stale(target, lang)
        return
    if day is None or day < clock.local_today():
        await _reply(target, lang, t(lang, "admin.to.bad_date"))
        return
    data = await state.get_data()
    tid = int(data["target"])
    await state.update_data(start_day=day.isoformat())
    await state.set_state(AdminTimeOffFlow.end_day)
    extra = [btn(t(lang, "admin.to.single_day"), acb(S, "ed", tid, day.strftime("%Y%m%d")))]
    text = t(lang, "admin.to.ask_end", day=fmt_date(day, lang))
    await screen.render(
        target, state, text, _days_kb(lang, "ed", tid, day + timedelta(days=1), extra=extra)
    )


async def _pick_end(target: Target, state: FSMContext, lang: str, day: date | None) -> None:
    if await state.get_state() != AdminTimeOffFlow.end_day.state:
        if isinstance(target, CallbackQuery):
            await views.stale(target, lang)
        return
    data = await state.get_data()
    start_day = date.fromisoformat(data["start_day"])
    if day is None or day < start_day:
        await _reply(target, lang, t(lang, "admin.to.bad_date"))
        return
    await state.update_data(end_day=day.isoformat())
    if day == start_day:
        await state.set_state(AdminTimeOffFlow.hours)
        rows = [
            [btn(t(lang, "admin.to.whole_day"), acb(S, "whole", int(data["target"])))],
            [btn(t(lang, "common.cancel"), acb(S, "tgt", int(data["target"])))],
        ]
        await screen.render(
            target, state, t(lang, "admin.to.ask_hours", day=fmt_date(day, lang)), kb(rows)
        )
        return
    start = schedule.full_day_interval(start_day).start
    end = schedule.full_day_interval(day).end
    await _ask_reason(target, state, lang, start, end)


async def _reply(target: Target, lang: str, text: str) -> None:
    if isinstance(target, CallbackQuery):
        await views.alert(target, text)
    else:
        await target.answer(text)


async def _ask_reason(
    target: Target, state: FSMContext, lang: str, start: datetime, end: datetime
) -> None:
    data = await state.get_data()
    await state.update_data(start_at=start.isoformat(), end_at=end.isoformat())
    await state.set_state(AdminTimeOffFlow.reason)
    rows = [
        [btn(t(lang, "barber.block_no_reason"), acb(S, "rs", int(data["target"])))],
        [btn(t(lang, "common.cancel"), acb(S, "tgt", int(data["target"])))],
    ]
    text = t(lang, "admin.to.ask_reason", start=fmt_dt(start, lang), end=fmt_dt(end, lang))
    await screen.render(target, state, text, kb(rows))


@router.message(StateFilter(AdminTimeOffFlow.start_day), F.text)
async def on_start_text(message: Message, state: FSMContext, lang: str) -> None:
    await _pick_start(
        message, state, lang, views.parse_date_text(message.text or "", clock.local_today())
    )


@router.message(StateFilter(AdminTimeOffFlow.end_day), F.text)
async def on_end_text(message: Message, state: FSMContext, lang: str) -> None:
    await _pick_end(
        message, state, lang, views.parse_date_text(message.text or "", clock.local_today())
    )


@router.message(StateFilter(AdminTimeOffFlow.hours), F.text)
async def on_hours_text(message: Message, state: FSMContext, lang: str) -> None:
    rng = views.parse_time_range(message.text or "")
    if rng is None:
        await message.answer(t(lang, "admin.wh.bad_range"))
        return
    data = await state.get_data()
    day = date.fromisoformat(data["start_day"])
    await _ask_reason(
        message, state, lang, clock.local_dt(day, rng[0]), clock.local_dt(day, rng[1])
    )


@router.message(StateFilter(AdminTimeOffFlow.reason), F.text)
async def on_reason_text(
    message: Message, state: FSMContext, sm: SM, notifier: Notifier, user: Any, lang: str
) -> None:
    try:
        reason = validation.validate_comment(message.text or "")
    except InputError as e:
        await message.answer(views.err_text(lang, e))
        return
    await _preview(message, state, sm, notifier, user, lang, (reason or "")[:200] or None)


async def _preview(
    target: Target,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
    reason: str | None,
) -> None:
    data = await state.get_data()
    await state.update_data(reason=reason)
    tid = int(data["target"])
    start = datetime.fromisoformat(data["start_at"])
    end = datetime.fromisoformat(data["end_at"])
    try:
        async with read_tx(sm) as s:
            conflicts = await schedule.preview_time_off_conflicts(
                s, actor_user_id=user.id, barber_id=tid or None, start_at=start, end_at=end
            )
    except ServiceError as e:
        await state.clear()
        await screen.render(target, None, views.err_text(lang, e))
        return
    if not conflicts:
        await _save(target, state, sm, notifier, user, lang, cancel_conflicts=False)
        return
    lines = [t(lang, "barber.block_conflicts", n=len(conflicts))]
    for b in conflicts[:30]:
        phone = f" · {h(b.client_phone)}" if b.client_phone else ""
        lines.append(
            f"• {fmt_dt_short(b.start_at, lang)} {h(b.barber_name)} — {h(b.client_name)}{phone}"
        )
    rows = [
        [btn(t(lang, "barber.block_cancel_notify"), acb(S, "cf", tid, "1"))],
        [btn(t(lang, "barber.block_keep"), acb(S, "cf", tid, "0"))],
        [btn(t(lang, "common.cancel"), acb(S, "tgt", tid))],
    ]
    await state.set_state(AdminTimeOffFlow.confirm_conflicts)
    await screen.render(target, state, "\n".join(lines), kb(rows))


async def _save(
    target: Target,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
    *,
    cancel_conflicts: bool,
) -> None:
    data = await state.get_data()
    tid = int(data["target"])
    try:
        async with write_tx(sm) as s:
            res = await schedule.add_time_off(
                s,
                actor_user_id=user.id,
                barber_id=tid or None,
                start_at=datetime.fromisoformat(data["start_at"]),
                end_at=datetime.fromisoformat(data["end_at"]),
                reason=data.get("reason"),
                cancel_conflicts=cancel_conflicts,
            )
    except ServiceError as e:
        await state.clear()
        await screen.render(target, None, views.err_text(lang, e))
        return
    await state.clear()
    notes = [
        t(
            lang,
            "barber.block_saved",
            start=fmt_dt(res.time_off.start_at, lang),
            end=fmt_dt(res.time_off.end_at, lang),
        )
    ]
    if res.cancelled:
        notes.append(t(lang, "barber.block_cancelled_n", n=len(res.cancelled)))
    if res.overlapping_kept:
        notes.append(t(lang, "barber.block_kept_n", n=len(res.overlapping_kept)))
    await show_target(target, sm, lang, tid, note="\n".join(notes))
    if res.cancelled:
        await notifier.time_off_cancellations(res.cancelled)


@router.message(StateFilter(AdminTimeOffFlow))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
