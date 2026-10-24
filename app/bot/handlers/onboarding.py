"""/start (payloads), language, consent, exits and every main-menu reply button.

This router is included first and its exits use ``StateFilter("*")``, so the
menu buttons, /start, /menu and /cancel always win over FSM text input [E1].
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import NOOP, ConsentCb, DelMeCb, HubCb, LangCb, RoleCb
from app.bot.commands import set_user_commands
from app.bot.filters import IsAdmin, IsBarber
from app.bot.i18n import h, labels, norm_lang, t
from app.bot.keyboards.common import btn, kb, lang_picker, menu_for, role_picker
from app.bot.notifier import Notifier
from app.config import CONSENT_VERSION
from app.db.uow import read_tx, write_tx
from app.services import barbers, users
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="onboarding")
router.message.filter(F.chat.type == "private")

ANY = StateFilter("*")

SM = async_sessionmaker[AsyncSession]


# ------------------------------------------------------------------ /start


@router.message(CommandStart(), ANY)
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    roles: Any,
    lang: str,
    bot: Bot,
) -> None:
    await state.clear()
    payload = (command.args or "").strip()
    if payload.startswith("inv_"):
        await _link_request(message, sm, notifier, user, lang, payload[4:])
        return
    if payload.startswith("setup_"):
        await _claim_setup(message, sm, user, lang, payload[6:], bot)
        # fall through to the normal start screen with the new roles
        async with read_tx(sm) as s:
            ctx = await users.load_context(s, tg_id=message.from_user.id)
        if ctx is not None:
            user, roles = ctx.user, ctx.roles
    if (user.consent_version or 0) < CONSENT_VERSION:
        await views.send_onboarding(
            bot, message.chat.id, user, lang, message.from_user.language_code
        )
        return
    await views.send_home(message, state, sm, user, roles, lang)


async def _link_request(
    message: Message, sm: SM, notifier: Notifier, user: Any, lang: str, token: str
) -> None:
    try:
        async with write_tx(sm) as s:
            req = await barbers.request_link(s, user_id=user.id, token=token)
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await message.answer(t(lang, "barber.link_wait", name=h(req.barber.display_name)))
    await notifier.link_requested(req)


async def _claim_setup(
    message: Message, sm: SM, user: Any, lang: str, token: str, bot: Bot
) -> None:
    try:
        async with write_tx(sm) as s:
            await users.claim_setup_token(s, user_id=user.id, token=token)
            roles = await users.get_roles(s, user.id)
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await message.answer(t(lang, "admin.setup_done"))
    await set_user_commands(bot, message.from_user.id, lang, roles)


# ------------------------------------------------------------------ language / consent


@router.message(Command("lang"), ANY)
async def cmd_lang(message: Message, state: FSMContext, lang: str) -> None:
    await state.clear()
    await message.answer(t(lang, "onb.choose_lang"), reply_markup=lang_picker())


@router.callback_query(LangCb.filter(), ANY)
async def on_lang(
    call: CallbackQuery,
    callback_data: LangCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    bot: Bot,
) -> None:
    new_lang = norm_lang(callback_data.l)
    async with write_tx(sm) as s:
        user = await users.set_lang(s, user_id=user.id, lang=new_lang)
    lang = norm_lang(user.lang)
    if (user.consent_version or 0) < CONSENT_VERSION:
        await screen.render(call, None, t(lang, "onb.consent"), views.consent_kb(lang))
        return
    await set_user_commands(bot, call.from_user.id, lang, roles)
    await screen.safe_answer(call, t(lang, "onb.lang_set"))
    await screen.send_reply_kb(
        bot, call.from_user.id, t(lang, "onb.lang_set"), menu_for(roles, lang)
    )
    await views.show_hub(call, state, sm, user, roles, lang)


@router.callback_query(ConsentCb.filter(), ANY)
async def on_consent(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str, bot: Bot
) -> None:
    async with write_tx(sm) as s:
        user = await users.accept_consent(s, user_id=user.id, version=CONSENT_VERSION)
    await state.clear()
    await screen.send_reply_kb(bot, call.from_user.id, t(lang, "onb.thanks"), menu_for(roles, lang))
    await set_user_commands(bot, call.from_user.id, lang, roles)
    await views.show_hub(call, state, sm, user, roles, lang)


# ------------------------------------------------------------------ exits


@router.message(Command("menu"), ANY)
async def cmd_menu(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    await _leave_flow(message, state)
    await views.send_home(message, state, sm, user, roles, lang)


@router.message(Command("cancel"), ANY)
async def cmd_cancel(message: Message, state: FSMContext, roles: Any, lang: str) -> None:
    await _leave_flow(message, state)
    await message.answer(t(lang, "common.cancelled"), reply_markup=menu_for(roles, lang))


@router.callback_query(HubCb.filter(F.a == "cx"), ANY)
async def on_flow_cancel(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    """Inline ❌ on every text-input prompt [E1][#9]."""
    await state.clear()
    await screen.render(call, None, t(lang, "common.cancelled"))


async def _leave_flow(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("msg_id"):
        await screen.strip_keyboard(message.bot, message.chat.id, data["msg_id"])  # type: ignore[arg-type]


@router.message(Command("whoami"), ANY)
async def cmd_whoami(message: Message, user: Any, roles: Any, lang: str) -> None:
    role_names = [t(lang, "role.client")]
    if roles.is_barber:
        role_names.append(t(lang, "role.barber"))
    if roles.is_admin:
        role_names.append(t(lang, "role.admin") + (" (env)" if roles.is_env_admin else ""))
    await message.answer(
        t(
            lang,
            "onb.whoami",
            tg_id=message.from_user.id,
            uid=user.id,
            name=h(user.display_name),
            roles=", ".join(role_names),
            language=lang,
        )
    )


@router.message(Command("help"), ANY)
async def cmd_help(message: Message, roles: Any, lang: str) -> None:
    parts = [t(lang, "help.client")]
    if roles.is_barber:
        parts.append(t(lang, "help.barber"))
    if roles.is_admin:
        parts.append(t(lang, "help.admin"))
    await message.answer("\n\n".join(parts))


# ------------------------------------------------------------------ /delete_me [H1]


@router.message(Command("delete_me"), ANY)
async def cmd_delete_me(message: Message, state: FSMContext, lang: str) -> None:
    await state.clear()
    await message.answer(
        t(lang, "onb.delete_confirm"),
        reply_markup=kb(
            [
                [
                    btn(t(lang, "onb.delete_yes"), DelMeCb(a="yes")),
                    btn(t(lang, "common.no"), DelMeCb(a="no")),
                ]
            ]
        ),
    )


@router.callback_query(DelMeCb.filter(), ANY)
async def on_delete_me(
    call: CallbackQuery,
    callback_data: DelMeCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    if callback_data.a != "yes":
        await screen.render(call, None, t(lang, "common.cancelled"))
        return
    try:
        async with write_tx(sm) as s:
            cancelled = await users.delete_me(s, user_id=user.id)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    await state.clear()
    await screen.render(call, None, t(lang, "onb.deleted"))
    await screen.send_reply_kb(call.bot, call.from_user.id, "👋", ReplyKeyboardRemove())  # type: ignore[arg-type]
    for b in cancelled:
        await notifier.booking_cancelled(b, by_client=True)


# ------------------------------------------------------------------ role switch


@router.message(F.text.in_(labels("menu.role")), ANY)
async def on_role_button(message: Message, state: FSMContext, roles: Any, lang: str) -> None:
    await _leave_flow(message, state)
    await message.answer(t(lang, "role.pick"), reply_markup=role_picker(lang, roles))


@router.callback_query(RoleCb.filter(), ANY)
async def on_role(
    call: CallbackQuery,
    callback_data: RoleCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
    bot: Bot,
) -> None:
    view = callback_data.r
    allowed = {
        "c": True,
        "b": bool(roles.is_barber and roles.barber_id),
        "a": bool(roles.is_admin),
    }
    if not allowed.get(view, False):
        await views.stale(call, lang)
        return
    await state.clear()
    await screen.render(call, None, t(lang, f"role.now_{view}"))
    await screen.send_reply_kb(
        bot, call.from_user.id, t(lang, f"home.{view}"), menu_for(roles, lang, view)
    )
    if view == "c":
        # an old (>48h) picker is an InaccessibleMessage: open the hub as a new message
        target = call.message if isinstance(call.message, Message) else call
        await views.show_hub(target, state, sm, user, roles, lang, new_message=True)


@router.message(F.text.in_(labels("menu.b_client")), ANY)
async def on_client_view(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    await _leave_flow(message, state)
    await views.send_home(message, state, sm, user, roles, lang, view="c")


# ------------------------------------------------------------------ client menu buttons


@router.message(F.text.in_(labels("menu.book")), ANY)
async def on_book(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    from app.bot.handlers import client_booking

    await _leave_flow(message, state)
    await client_booking.start_booking(message, state, sm, user, lang, entry="service_first")


@router.message(F.text.in_(labels("menu.my")), ANY)
@router.message(Command("my"), ANY)
async def on_my(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    from app.bot.handlers import client_my

    await _leave_flow(message, state)
    await client_my.show_my(message, state, sm, user, lang)


@router.message(F.text.in_(labels("menu.bonus")), ANY)
async def on_bonus(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    from app.bot.handlers import client_info

    await _leave_flow(message, state)
    await client_info.show_bonus(message, state, sm, user, lang)


@router.message(F.text.in_(labels("menu.prices")), ANY)
async def on_prices(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    from app.bot.handlers import client_info

    await _leave_flow(message, state)
    await client_info.show_prices(message, state, sm, lang)


@router.message(F.text.in_(labels("menu.info")), ANY)
async def on_info(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    from app.bot.handlers import client_info

    await _leave_flow(message, state)
    await client_info.show_address(message, state, sm, lang)


# ------------------------------------------------------------------ barber menu buttons


@router.message(F.text.in_(labels("menu.b_today")), IsBarber(), ANY)
@router.message(Command("today"), IsBarber(), ANY)
async def on_today(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    from app.bot.handlers import barber_queue

    await _leave_flow(message, state)
    await barber_queue.show_day(message, state, sm, user, roles, lang, day=None)


@router.message(F.text.in_(labels("menu.b_upcoming")), IsBarber(), ANY)
async def on_upcoming(message: Message, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    from app.bot.handlers import barber_queue

    await _leave_flow(message, state)
    await barber_queue.show_day_picker(message, state, sm, lang)


@router.message(F.text.in_(labels("menu.b_busy")), IsBarber(), ANY)
async def on_busy(message: Message, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    from app.bot.handlers import barber_block

    await _leave_flow(message, state)
    await barber_block.show_busy(message, lang, sm, roles)


@router.message(F.text.in_(labels("menu.b_block")), IsBarber(), ANY)
async def on_block(message: Message, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    from app.bot.handlers import barber_block

    await _leave_flow(message, state)
    await barber_block.start_block(message, state, sm, roles, lang)


@router.message(F.text.in_(labels("menu.b_manual")), IsBarber(), ANY)
async def on_manual(message: Message, state: FSMContext, sm: SM, roles: Any, lang: str) -> None:
    from app.bot.handlers import barber_manual

    await _leave_flow(message, state)
    await barber_manual.start_manual(message, state, sm, roles, lang)


# ------------------------------------------------------------------ admin menu buttons

_ADMIN_SECTIONS = {
    "menu.a_barbers": ("admin_barbers", "show_list"),
    "menu.a_services": ("admin_services", "show_list"),
    "menu.a_hours": ("admin_hours", "show_pick_barber"),
    "menu.a_timeoff": ("admin_timeoff", "show_targets"),
    "menu.a_bookings": ("admin_bookings", "show_list"),
    "menu.a_clients": ("admin_clients", "start_search"),
    "menu.a_stats": ("admin_stats", "show_menu"),
    "menu.a_settings": ("admin_settings", "show_groups"),
    "menu.a_admins": ("admin_admins", "show_list"),
}

_ADMIN_LABELS: dict[str, str] = {}
for _key in _ADMIN_SECTIONS:
    for _label in labels(_key):
        _ADMIN_LABELS[_label] = _key


@router.message(F.text.in_(set(_ADMIN_LABELS)), IsAdmin(), ANY)
async def on_admin_section(
    message: Message, state: FSMContext, sm: SM, user: Any, roles: Any, lang: str
) -> None:
    import importlib

    await _leave_flow(message, state)
    module_name, fn_name = _ADMIN_SECTIONS[_ADMIN_LABELS[message.text or ""]]
    module = importlib.import_module(f"app.bot.handlers.{module_name}")
    await getattr(module, fn_name)(message, state, sm, user, lang)


# ------------------------------------------------------------------ noop


@router.callback_query(F.data == NOOP, ANY)
async def on_noop(call: CallbackQuery) -> None:
    await screen.safe_answer(call)
