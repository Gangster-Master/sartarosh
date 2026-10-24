"""Admin → ⚙️ Sozlamalar: grouped by SETTING_SPECS.group, typed input,
bool toggles, choices, and the shop location share."""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, kb, location_request, menu_for
from app.bot.states import AdminSettingsFlow
from app.db.uow import read_tx, write_tx
from app.services import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_settings")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "st"
GROUPS = ("booking", "pending", "reminders", "noshow", "loyalty", "staff", "jobs", "shop")
LOCATION_KEY = "__location"


def _specs() -> dict[str, Any]:
    return {k: v for k, v in settings.SETTING_SPECS.items() if not k.startswith("_")}


def _group_keys(group: str) -> list[str]:
    return [k for k, spec in _specs().items() if getattr(spec, "group", None) == group]


def _value_text(value: Any, lang: str) -> str:
    if isinstance(value, bool):
        return "✅" if value else "▫️"
    if value is None or value == "":
        return "—"
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


def _choice_label(key: str, value: str, lang: str) -> str:
    k = f"settings.choice.{key}.{value}"
    from app.bot.i18n import has_key

    return t(lang, k) if has_key(k) else value


async def show_groups(
    target: Target,
    state: FSMContext | None,
    sm: SM,
    user: Any,
    lang: str,
    *,
    note: str | None = None,
) -> None:
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(t(lang, "admin.st.title"))
    rows = [
        [btn(t(lang, f"settings.group.{g}"), acb(S, "grp", 0, g))] for g in GROUPS if _group_keys(g)
    ]
    rows.append([btn(t(lang, "admin.st.location"), acb(S, "loc"))])
    await screen.render(target, None, "\n".join(lines), kb(rows))


async def show_group(
    target: Target, sm: SM, lang: str, group: str, *, note: str | None = None
) -> None:
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(f"<b>{t(lang, f'settings.group.{group}')}</b>")
    lines.append("")
    rows = []
    for key in _group_keys(group):
        spec = _specs()[key]
        value = getattr(shop, key, None)
        shown = (
            _choice_label(key, str(value), lang)
            if spec.type == "choice"
            else _value_text(value, lang)
        )
        lines.append(f"• {t(lang, f'settings.{key}')}: <b>{h(shown)}</b>")
        label = f"{t(lang, f'settings.{key}')}: {shown}"
        if spec.type == "bool":
            rows.append([btn(label, acb(S, "tgl", 0, key))])
        elif spec.type == "choice":
            rows.append([btn(label, acb(S, "ch", 0, key))])
        else:
            rows.append([btn(label, acb(S, "key", 0, key))])
    rows.append([back_btn(lang, S, "groups")])
    await screen.render(target, None, "\n".join(lines), kb(rows))


async def _update(sm: SM, user: Any, key: str, raw: str) -> None:
    async with write_tx(sm) as s:
        await settings.update_setting(s, actor_user_id=user.id, key=key, raw_value=raw)


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_settings(
    call: CallbackQuery,
    callback_data: AdminCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    bot: Bot,
) -> None:
    a, v = callback_data.a, callback_data.v
    specs = _specs()
    try:
        if a == "groups":
            await state.clear()
            await show_groups(call, state, sm, user, lang)
        elif a == "grp":
            if v not in GROUPS:
                await views.stale(call, lang)
                return
            await state.clear()
            await show_group(call, sm, lang, v)
        elif a == "tgl":
            spec = specs.get(v)
            if spec is None or spec.type != "bool":
                await views.stale(call, lang)
                return
            async with read_tx(sm) as s:
                current = bool(getattr(await settings.get_shop_settings(s), v))
            await _update(sm, user, v, "false" if current else "true")
            await show_group(call, sm, lang, spec.group, note=t(lang, "admin.saved"))
        elif a == "ch":
            spec = specs.get(v)
            if spec is None or spec.type != "choice":
                await views.stale(call, lang)
                return
            rows = [
                [btn(_choice_label(v, c, lang), acb(S, "setc", 0, f"{v}={c}"))]
                for c in spec.choices
            ]
            rows.append([back_btn(lang, S, "grp", 0, spec.group)])
            await screen.render(
                call, None, t(lang, "admin.st.pick_choice", name=t(lang, f"settings.{v}")), kb(rows)
            )
        elif a == "setc":
            key, _, choice = v.partition("=")
            spec = specs.get(key)
            if spec is None or choice not in spec.choices:
                await views.stale(call, lang)
                return
            await _update(sm, user, key, choice)
            await show_group(call, sm, lang, spec.group, note=t(lang, "admin.saved"))
        elif a == "key":
            spec = specs.get(v)
            if spec is None:
                await views.stale(call, lang)
                return
            await state.clear()
            await state.set_state(AdminSettingsFlow.value)
            await state.update_data(key=v)
            hint_key = f"admin.st.hint_{spec.type}"
            hint = t(lang, hint_key, min=_num(spec.min), max=_num(spec.max))
            text = t(lang, "admin.st.ask_value", name=t(lang, f"settings.{v}")) + "\n" + hint
            await screen.render(call, state, text, input_cancel_kb(lang))
        elif a == "loc":
            await state.clear()
            await state.set_state(AdminSettingsFlow.value)
            await state.update_data(key=LOCATION_KEY)
            await screen.render(call, None, t(lang, "admin.st.loc_prompt"), input_cancel_kb(lang))
            await screen.send_reply_kb(
                bot, call.from_user.id, t(lang, "admin.st.loc_hint"), location_request(lang)
            )
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


def _num(v: float | None) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else str(v)


@router.message(StateFilter(AdminSettingsFlow.value), F.location)
async def on_location(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    data = await state.get_data()
    if data.get("key") != LOCATION_KEY or message.location is None:
        await message.answer(t(lang, "common.send_text"))
        return
    try:
        async with write_tx(sm) as s:
            await settings.set_location(
                s,
                actor_user_id=user.id,
                latitude=message.location.latitude,
                longitude=message.location.longitude,
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await message.answer(t(lang, "admin.st.loc_saved"), reply_markup=menu_for(roles, lang, "a"))


@router.message(StateFilter(AdminSettingsFlow.value), F.text)
async def on_value(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    data = await state.get_data()
    key = data.get("key")
    if key == LOCATION_KEY:
        # "lat, lon" typed by hand is accepted too
        parts = (message.text or "").replace(";", ",").split(",")
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            await message.answer(t(lang, "admin.st.loc_prompt"))
            return
        try:
            async with write_tx(sm) as s:
                await settings.set_location(s, actor_user_id=user.id, latitude=lat, longitude=lon)
        except ServiceError as e:
            await message.answer(views.err_text(lang, e))
            return
        await state.clear()
        await message.answer(t(lang, "admin.st.loc_saved"), reply_markup=menu_for(roles, lang, "a"))
        return
    spec = _specs().get(str(key))
    if spec is None:
        await state.clear()
        return
    try:
        await _update(sm, user, str(key), message.text or "")
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await show_group(message, sm, lang, spec.group, note=t(lang, "admin.saved"))


@router.message(StateFilter(AdminSettingsFlow.value))
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
