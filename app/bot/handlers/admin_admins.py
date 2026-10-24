"""Admin → 🛡 Adminlar: list (env + promoted), promote by forwarded message or
tg id, demote promoted admins [G2]; barber link approvals [D4]."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb, LinkCb
from app.bot.commands import set_user_commands
from app.bot.filters import IsAdmin
from app.bot.i18n import h, norm_lang, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb
from app.bot.keyboards.common import btn, kb, menu_for
from app.bot.notifier import Notifier
from app.bot.states import AdminAdminsFlow
from app.config import get_config
from app.db.uow import read_tx, write_tx
from app.services import barbers, schedule, users
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_admins")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "adm"


async def show_list(
    target: Target,
    state: FSMContext | None,
    sm: SM,
    user: Any,
    lang: str,
    *,
    note: str | None = None,
) -> None:
    env_ids = get_config().admin_ids
    async with read_tx(sm) as s:
        admins = await users.list_admins(s, actor_user_id=user.id)
        pending = [
            b for b in await barbers.list_barbers(s, public_only=False) if b.pending_link_user_id
        ]
        pending_users = {b.id: await users.get_user(s, b.pending_link_user_id) for b in pending}
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(t(lang, "admin.adm.title"))
    rows = []
    for u in admins:
        is_env = u.tg_id in env_ids
        badge = t(lang, "admin.adm.env") if is_env else t(lang, "admin.adm.promoted")
        uname = f" @{h(u.tg_username)}" if u.tg_username else ""
        lines.append(f"• {h(u.display_name)}{uname} · <code>{u.tg_id}</code> · {badge}")
        if not is_env and u.id != user.id:
            rows.append(
                [btn(t(lang, "admin.adm.demote", name=u.display_name), acb(S, "del", u.id))]
            )
    if pending:
        lines.extend(["", t(lang, "admin.adm.pending_links")])
        for b in pending:
            pu = pending_users[b.id]
            lines.append(
                f"• {h(pu.display_name)} (<code>{pu.tg_id}</code>) → 💈 {h(b.display_name)}"
            )
            rows.append(
                [
                    btn(f"✅ {b.display_name}", LinkCb(a="ok", b=b.id, u=pu.id)),
                    btn(f"❌ {b.display_name}", LinkCb(a="no", b=b.id, u=pu.id)),
                ]
            )
    rows.append([btn(t(lang, "admin.adm.promote"), acb(S, "new"))])
    await screen.render(target, None, "\n".join(lines), kb(rows))


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_admins(
    call: CallbackQuery,
    callback_data: AdminCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    bot: Bot,
) -> None:
    a, uid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "list":
            await state.clear()
            await show_list(call, state, sm, user, lang)
        elif a == "new":
            await state.clear()
            await state.set_state(AdminAdminsFlow.promote)
            await screen.render(call, state, t(lang, "admin.adm.ask_user"), input_cancel_kb(lang))
        elif a == "del":
            async with read_tx(sm) as s:
                target_user = await users.get_user(s, uid)
                target_roles = await users.get_roles(s, uid)
            if not target_user.is_admin and not target_roles.is_env_admin:
                # stale/repeated confirm: not a (bot-made) admin any more, tell nobody
                await views.stale(call, lang)
                await show_list(call, state, sm, user, lang)
                return
            if v != "ok":
                markup = kb(
                    [[btn(t(lang, "common.yes"), acb(S, "del", uid, "ok")), back_btn(lang, S)]]
                )
                await screen.render(
                    call,
                    None,
                    t(lang, "admin.adm.demote_confirm", name=h(target_user.display_name)),
                    markup,
                )
                return
            async with write_tx(sm) as s:
                await users.set_admin(s, actor_user_id=user.id, user_id=uid, is_admin=False)
                roles = await users.get_roles(s, uid)
            await show_list(call, state, sm, user, lang, note=t(lang, "admin.adm.demoted"))
            tlang = norm_lang(target_user.lang)
            await set_user_commands(bot, target_user.tg_id, tlang, roles)
            await screen.send_reply_kb(
                bot, target_user.tg_id, t(tlang, "admin.adm.you_demoted"), menu_for(roles, tlang)
            )
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


@router.message(StateFilter(AdminAdminsFlow.promote))
async def on_promote(
    message: Message, state: FSMContext, sm: SM, user: Any, lang: str, bot: Bot
) -> None:
    tg_id: int | None = None
    if (
        message.forward_origin is not None
        and getattr(message.forward_origin, "sender_user", None) is not None
    ):
        tg_id = message.forward_origin.sender_user.id  # type: ignore[union-attr]
    elif message.text and message.text.strip().lstrip("-").isdigit():
        tg_id = int(message.text.strip())
    if tg_id is None:
        await message.answer(t(lang, "admin.adm.ask_user"))
        return
    async with read_tx(sm) as s:
        ctx = await users.load_context(s, tg_id=tg_id)
    if ctx is None:
        await message.answer(t(lang, "admin.adm.unknown_user"))
        return
    try:
        async with write_tx(sm) as s:
            await users.set_admin(s, actor_user_id=user.id, user_id=ctx.user.id, is_admin=True)
            roles = await users.get_roles(s, ctx.user.id)
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await show_list(
        message,
        state,
        sm,
        user,
        lang,
        note=t(lang, "admin.adm.promoted_ok", name=h(ctx.user.display_name)),
    )
    tlang = norm_lang(ctx.user.lang)
    await set_user_commands(bot, tg_id, tlang, roles)
    await screen.send_reply_kb(
        bot, tg_id, t(tlang, "admin.adm.you_promoted"), menu_for(roles, tlang, "a")
    )


@router.callback_query(LinkCb.filter(), ANY)
async def on_link_decision(
    call: CallbackQuery, callback_data: LinkCb, sm: SM, notifier: Notifier, user: Any, lang: str
) -> None:
    approve = callback_data.a == "ok"
    try:
        async with write_tx(sm) as s:
            if approve:
                barber = await barbers.approve_link(
                    s, actor_user_id=user.id, barber_id=callback_data.b, user_id=callback_data.u
                )
            else:
                barber = await barbers.deny_link(
                    s, actor_user_id=user.id, barber_id=callback_data.b, user_id=callback_data.u
                )
            linked_user = await users.get_user(s, callback_data.u)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        await screen.render(call, None, t(lang, "common.stale"))
        return
    key = "admin.adm.link_approved" if approve else "admin.adm.link_denied"
    text = t(lang, key, user=h(linked_user.display_name), barber=h(barber.display_name))
    markup = None
    if approve:  # a new barber is invisible to clients until he has hours
        async with read_tx(sm) as s:
            has_hours = bool(await schedule.get_week_hours(s, barber.id))
        if not has_hours:
            text += "\n\n" + t(lang, "admin.br.no_hours")
            markup = kb(
                [[btn(t(lang, "admin.br.set_hours_now"), AdminCb(s="wh", a="pick", id=barber.id))]]
            )
    await screen.render(call, None, text, markup)
    await notifier.link_decided(barber, linked_user.tg_id, approve)
