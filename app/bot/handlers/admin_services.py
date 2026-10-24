"""Admin → 🧾 Xizmatlar: create, edit name/duration/price, activate/deactivate [#74]."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_price, h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.states import AdminServiceFlow
from app.db.uow import read_tx, write_tx
from app.services import catalog
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_services")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "sv"
DURATIONS = (15, 30, 45, 60, 90, 120)


async def show_list(
    target: Target,
    state: FSMContext | None,
    sm: SM,
    user: Any,
    lang: str,
    *,
    note: str | None = None,
) -> None:
    async with read_tx(sm) as s:
        items = await catalog.list_services(s, active_only=False)
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(t(lang, "admin.sv.title"))
    rows = []
    for x in items:
        mark = "" if x.is_active else "⏸ "
        lines.append(
            f"{mark}{h(x.name)} — {fmt_price(x.price_uzs, lang)} · {t(lang, 'common.minutes', n=x.duration_min)}"
        )
        rows.append([btn(f"{mark}{x.name}", acb(S, "pick", x.id))])
    if not items:
        lines.append(t(lang, "admin.sv.empty"))
    rows.append([btn(t(lang, "admin.sv.new"), acb(S, "new"))])
    await screen.render(target, None, "\n".join(lines), kb(rows))


async def _card(target: Target, sm: SM, lang: str, sid: int, *, note: str | None = None) -> None:
    async with read_tx(sm) as s:
        x = next(
            (y for y in await catalog.list_services(s, active_only=False) if y.id == sid), None
        )
    if x is None:
        if isinstance(target, CallbackQuery):
            await views.stale(target, lang)
        return
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(f"🧾 <b>{h(x.name)}</b>")
    lines.append(t(lang, "admin.sv.duration", n=x.duration_min))
    lines.append(t(lang, "admin.sv.price", price=fmt_price(x.price_uzs, lang)))
    lines.append(t(lang, "admin.sv.active") if x.is_active else t(lang, "admin.sv.inactive"))
    rows = [
        [btn(t(lang, "admin.sv.edit_name"), acb(S, "edit", x.id, "name"))],
        [
            btn(t(lang, "admin.sv.edit_duration"), acb(S, "edit", x.id, "duration")),
            btn(t(lang, "admin.sv.edit_price"), acb(S, "edit", x.id, "price")),
        ],
        [
            btn(
                t(lang, "admin.sv.deactivate") if x.is_active else t(lang, "admin.sv.activate"),
                acb(S, "tgl", x.id),
            )
        ],
        [back_btn(lang, S)],
    ]
    await screen.render(target, None, "\n".join(lines), kb(rows))


def _duration_kb(lang: str, a: str, sid: int = 0) -> Any:
    buttons = [btn(t(lang, "common.minutes", n=n), acb(S, a, sid, str(n))) for n in DURATIONS]
    rows = chunk(buttons, 3)
    rows.append([btn(t(lang, "common.cancel"), acb(S, "list"))])
    return kb(rows)


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_services(
    call: CallbackQuery, callback_data: AdminCb, state: FSMContext, sm: SM, user: Any, lang: str
) -> None:
    a, sid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "list":
            await state.clear()
            await show_list(call, state, sm, user, lang)
        elif a == "pick":
            await state.clear()
            await _card(call, sm, lang, sid)
        elif a == "new":
            await state.clear()
            await state.set_state(AdminServiceFlow.name)
            await screen.render(call, state, t(lang, "admin.sv.ask_name"), input_cancel_kb(lang))
        elif a == "dur":  # duration button during creation
            if await state.get_state() != AdminServiceFlow.duration.state or not v.isdigit():
                await views.stale(call, lang)
                return
            await state.update_data(new_duration=int(v))
            await state.set_state(AdminServiceFlow.price)
            await screen.render(call, state, t(lang, "admin.sv.ask_price"), input_cancel_kb(lang))
        elif a == "edit":
            if v not in ("name", "duration", "price"):
                await views.stale(call, lang)
                return
            await state.clear()
            await state.set_state(AdminServiceFlow.edit_field)
            await state.update_data(service_id=sid, field=v)
            markup = _duration_kb(lang, "setdur", sid) if v == "duration" else input_cancel_kb(lang)
            await screen.render(call, state, t(lang, f"admin.sv.ask_{v}"), markup)
        elif a == "setdur":
            if not v.isdigit():
                await views.stale(call, lang)
                return
            async with write_tx(sm) as s:
                await catalog.update_service(
                    s, actor_user_id=user.id, service_id=sid, duration_min=int(v)
                )
            await state.clear()
            await _card(call, sm, lang, sid, note=t(lang, "admin.saved"))
        elif a == "tgl":
            async with read_tx(sm) as s:
                x = next(
                    (y for y in await catalog.list_services(s, active_only=False) if y.id == sid),
                    None,
                )
            if x is None:
                await views.stale(call, lang)
                return
            async with write_tx(sm) as s:
                await catalog.update_service(
                    s, actor_user_id=user.id, service_id=sid, is_active=not x.is_active
                )
            await _card(call, sm, lang, sid, note=t(lang, "admin.saved"))
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


def _parse_int(raw: str) -> int | None:
    s = raw.replace(" ", "").replace("_", "").replace(",", "").replace(".", "")
    for suffix in ("so'm", "som", "сум", "daq", "min", "мин"):
        s = s.lower().removesuffix(suffix)
    return int(s) if s.lstrip("-").isdigit() else None


@router.message(StateFilter(AdminServiceFlow.name), F.text)
async def on_name(message: Message, state: FSMContext, lang: str) -> None:
    name = " ".join((message.text or "").split())
    if not 2 <= len(name) <= 64:
        await message.answer(t(lang, "err.invalid.name"))
        return
    await state.update_data(new_name=name)
    await state.set_state(AdminServiceFlow.duration)
    await screen.render(message, state, t(lang, "admin.sv.ask_duration"), _duration_kb(lang, "dur"))


@router.message(StateFilter(AdminServiceFlow.duration), F.text)
async def on_duration(message: Message, state: FSMContext, lang: str) -> None:
    n = _parse_int(message.text or "")
    if n is None or n <= 0 or n % 15 or n > 240:
        await message.answer(t(lang, "err.invalid.duration_min"))
        return
    await state.update_data(new_duration=n)
    await state.set_state(AdminServiceFlow.price)
    await screen.render(message, state, t(lang, "admin.sv.ask_price"), input_cancel_kb(lang))


@router.message(StateFilter(AdminServiceFlow.price), F.text)
async def on_price(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    n = _parse_int(message.text or "")
    if n is None or n < 0:
        await message.answer(t(lang, "err.invalid.price_uzs"))
        return
    data = await state.get_data()
    try:
        async with write_tx(sm) as s:
            x = await catalog.create_service(
                s,
                actor_user_id=user.id,
                name=data["new_name"],
                duration_min=int(data["new_duration"]),
                price_uzs=n,
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await _card(message, sm, lang, x.id, note=t(lang, "admin.sv.created"))


@router.message(StateFilter(AdminServiceFlow.edit_field), F.text)
async def on_edit(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    data = await state.get_data()
    sid, field = int(data["service_id"]), data.get("field")
    raw = message.text or ""
    kwargs: dict[str, Any] = {}
    if field == "name":
        kwargs["name"] = " ".join(raw.split())
    else:
        n = _parse_int(raw)
        if n is None:
            await message.answer(t(lang, "err.invalid"))
            return
        kwargs["duration_min" if field == "duration" else "price_uzs"] = n
    try:
        async with write_tx(sm) as s:
            await catalog.update_service(s, actor_user_id=user.id, service_id=sid, **kwargs)
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await _card(message, sm, lang, sid, note=t(lang, "admin.saved"))


@router.message(StateFilter(AdminServiceFlow))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
