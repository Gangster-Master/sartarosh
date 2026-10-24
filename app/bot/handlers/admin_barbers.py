"""Admin → 💈 Sartaroshlar: create, edit, services, step/buffer, auto-accept,
invite link, link approvals (badges), unlink, deactivate (preview → reason),
activate [B4][B6][D4]."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.i18n import fmt_dt, fmt_dt_short, h, t
from app.bot.keyboards.admin import acb, back_btn, input_cancel_kb, onoff
from app.bot.keyboards.common import btn, chunk, kb
from app.bot.notifier import Notifier
from app.bot.states import AdminBarberFlow
from app.db.uow import read_tx, write_tx
from app.services import barbers, catalog, clock, users
from app.services.errors import NotFoundError, ServiceError

log = logging.getLogger(__name__)

router = Router(name="admin_barbers")
router.message.filter(F.chat.type == "private")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message
S = "br"


def _badges(b: Any) -> str:
    out = []
    if not b.is_active:
        out.append("⏸")
    if b.user_id is None:
        out.append("🔴")
    elif not b.is_reachable:
        out.append("⚠️")
    if b.pending_link_user_id:
        out.append("🔗")
    return " ".join(out)


async def show_list(target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str) -> None:
    async with read_tx(sm) as s:
        items = await barbers.list_barbers(s, public_only=False)
    lines = [t(lang, "admin.br.title"), "", t(lang, "admin.br.legend")]
    rows = []
    for b in items:
        badge = _badges(b)
        rows.append([btn(f"💈 {b.display_name} {badge}".strip(), acb(S, "pick", b.id))])
    if not items:
        lines.extend(["", t(lang, "admin.br.empty")])
    rows.append([btn(t(lang, "admin.br.new"), acb(S, "new"))])
    await screen.render(target, None, "\n".join(lines), kb(rows))


async def _invite_link(bot: Bot, token: str) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=inv_{token}"


async def show_card(
    target: Target, sm: SM, user: Any, lang: str, barber_id: int, *, note: str | None = None
) -> None:
    try:
        async with read_tx(sm) as s:
            b = await barbers.get_barber(s, barber_id)
            linked = await users.get_user(s, b.user_id) if b.user_id else None
            pending = (
                await users.get_user(s, b.pending_link_user_id) if b.pending_link_user_id else None
            )
            offered = await catalog.list_barber_services(s, b.id)
    except NotFoundError:
        if isinstance(target, CallbackQuery):
            await views.stale(target, lang)
        return
    lines = []
    if note:
        lines.extend([note, ""])
    lines.append(f"💈 <b>{h(b.display_name)}</b> {_badges(b)}")
    if b.bio:
        lines.append(h(b.bio))
    lines.append("")
    lines.append(
        t(lang, "admin.br.status_active") if b.is_active else t(lang, "admin.br.status_inactive")
    )
    if linked is not None:
        uname = f" @{h(linked.tg_username)}" if linked.tg_username else ""
        lines.append(
            t(
                lang,
                "admin.br.linked",
                name=h(linked.display_name),
                username=uname,
                tg_id=linked.tg_id,
            )
        )
        if not b.is_reachable:
            lines.append(t(lang, "admin.br.unreachable"))
    else:
        lines.append(t(lang, "admin.br.unlinked"))
    if pending is not None:
        lines.append(t(lang, "admin.br.pending", name=h(pending.display_name), tg_id=pending.tg_id))
    lines.append(t(lang, "admin.br.step", step=b.slot_step_min, buffer=b.buffer_min))
    lines.append(t(lang, "admin.br.auto", value=onoff(b.auto_accept)))
    names = ", ".join(h(x.name) for x in offered if x.name) or "—"
    lines.append(t(lang, "admin.br.services", names=names))
    if b.invite_token and b.user_id is None:
        exp = b.invite_expires_at
        if exp is None or exp > clock.now_utc():
            bot: Bot = target.bot  # type: ignore[assignment]
            link = await _invite_link(bot, b.invite_token)
            lines.append("")
            lines.append(
                t(lang, "admin.br.invite", link=h(link), until=fmt_dt(exp, lang) if exp else "—")
            )
        else:
            lines.append(t(lang, "admin.br.invite_expired"))
    if b.photo_file_id:
        lines.append(t(lang, "admin.br.has_photo"))

    rows = [
        [
            btn(t(lang, "admin.br.edit_name"), acb(S, "edit", b.id, "name")),
            btn(t(lang, "admin.br.edit_bio"), acb(S, "edit", b.id, "bio")),
        ],
        [
            btn(t(lang, "admin.br.edit_photo"), acb(S, "edit", b.id, "photo")),
            btn(t(lang, "admin.br.step_btn"), acb(S, "step", b.id)),
        ],
        [
            btn(
                f"{onoff(b.auto_accept)} {t(lang, 'admin.br.auto_btn')}",
                acb(S, "tgl", b.id, "auto"),
            ),
            btn(t(lang, "admin.br.services_btn"), acb(S, "svc", b.id)),
        ],
        [btn(t(lang, "admin.br.hours_btn"), AdminCb(s="wh", a="pick", id=b.id))],
    ]
    if b.user_id is None:
        rows.append([btn(t(lang, "admin.br.invite_btn"), acb(S, "inv", b.id))])
    else:
        rows.append([btn(t(lang, "admin.br.unlink_btn"), acb(S, "unl", b.id))])
    if b.is_active:
        rows.append([btn(t(lang, "admin.br.deact_btn"), acb(S, "deact", b.id))])
    else:
        rows.append([btn(t(lang, "admin.br.act_btn"), acb(S, "act", b.id))])
    rows.append([back_btn(lang, S)])
    await screen.render(target, None, "\n".join(lines), kb(rows))


# ------------------------------------------------------------------ callbacks


@router.callback_query(AdminCb.filter(F.s == S), ANY)
async def on_admin_barbers(
    call: CallbackQuery,
    callback_data: AdminCb,
    state: FSMContext,
    sm: SM,
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    a, bid, v = callback_data.a, callback_data.id, callback_data.v
    try:
        if a == "list":
            await state.clear()
            await show_list(call, state, sm, user, lang)
        elif a == "pick":
            await state.clear()
            await show_card(call, sm, user, lang, bid)
        elif a == "new":
            await state.clear()
            await state.set_state(AdminBarberFlow.name)
            await screen.render(call, state, t(lang, "admin.br.ask_name"), input_cancel_kb(lang))
        elif a == "skip":
            await _creation_skip(call, state, sm, user, lang, v)
        elif a == "edit":
            await _start_edit(call, state, lang, bid, v)
        elif a == "clr":
            if await state.get_state() != AdminBarberFlow.edit_field.state:
                await views.stale(call, lang)
                return
            await _apply_edit(call, state, sm, user, lang, bid, v, "")
        elif a == "step":
            await _step_menu(call, sm, lang, bid)
        elif a == "set":
            await _set_step(call, sm, user, lang, bid, v)
        elif a == "tgl":
            async with write_tx(sm) as s:
                b = await barbers.get_barber(s, bid)
                await barbers.update_barber(
                    s, actor_user_id=user.id, barber_id=bid, auto_accept=not b.auto_accept
                )
            await show_card(call, sm, user, lang, bid)
        elif a in ("svc", "svct", "svcs"):
            await _services(call, state, sm, user, lang, a, bid, v)
        elif a == "inv":
            async with write_tx(sm) as s:
                await barbers.regenerate_invite(s, actor_user_id=user.id, barber_id=bid)
            await show_card(call, sm, user, lang, bid, note=t(lang, "admin.br.invite_new"))
        elif a == "unl":
            if v != "ok":
                markup = kb(
                    [
                        [
                            btn(t(lang, "common.yes"), acb(S, "unl", bid, "ok")),
                            btn(t(lang, "common.no"), acb(S, "pick", bid)),
                        ]
                    ]
                )
                await screen.render(call, None, t(lang, "admin.br.unlink_confirm"), markup)
                return
            async with write_tx(sm) as s:
                await barbers.unlink_barber(s, actor_user_id=user.id, barber_id=bid)
            await show_card(call, sm, user, lang, bid, note=t(lang, "admin.br.unlinked_ok"))
        elif a == "deact":
            await _deactivate(call, state, sm, user, lang, bid, v)
        elif a == "act":
            async with write_tx(sm) as s:
                await barbers.activate_barber(s, actor_user_id=user.id, barber_id=bid)
            await show_card(call, sm, user, lang, bid, note=t(lang, "admin.br.activated"))
        else:
            await views.stale(call, lang)
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))


# ------------------------------------------------------------------ create


@router.message(StateFilter(AdminBarberFlow.name), F.text)
async def on_new_name(message: Message, state: FSMContext, lang: str) -> None:
    name = " ".join((message.text or "").split())
    if not 2 <= len(name) <= 64:
        await message.answer(t(lang, "err.invalid.display_name"))
        return
    await state.update_data(new_name=name)
    await state.set_state(AdminBarberFlow.bio)
    markup = kb(
        [
            [btn(t(lang, "common.skip"), acb(S, "skip", 0, "bio"))],
            [btn(t(lang, "common.cancel"), acb(S, "list"))],
        ]
    )
    await screen.render(message, state, t(lang, "admin.br.ask_bio"), markup)


@router.message(StateFilter(AdminBarberFlow.bio), F.text)
async def on_new_bio(message: Message, state: FSMContext, lang: str) -> None:
    bio = (message.text or "").strip()
    if len(bio) > 300:
        await message.answer(t(lang, "err.invalid.bio"))
        return
    await state.update_data(new_bio=bio or None)
    await _ask_photo(message, state, lang)


async def _ask_photo(target: Target, state: FSMContext, lang: str) -> None:
    await state.set_state(AdminBarberFlow.photo)
    markup = kb(
        [
            [btn(t(lang, "common.skip"), acb(S, "skip", 0, "photo"))],
            [btn(t(lang, "common.cancel"), acb(S, "list"))],
        ]
    )
    await screen.render(target, state, t(lang, "admin.br.ask_photo"), markup)


@router.message(StateFilter(AdminBarberFlow.photo), F.photo)
async def on_new_photo(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    assert message.photo
    await _create(message, state, sm, user, lang, message.photo[-1].file_id)


async def _creation_skip(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, lang: str, what: str
) -> None:
    current = await state.get_state()
    if what == "bio" and current == AdminBarberFlow.bio.state:
        await state.update_data(new_bio=None)
        await _ask_photo(call, state, lang)
    elif what == "photo" and current == AdminBarberFlow.photo.state:
        await _create(call, state, sm, user, lang, None)
    else:
        await views.stale(call, lang)


async def _create(
    target: Target, state: FSMContext, sm: SM, user: Any, lang: str, photo: str | None
) -> None:
    data = await state.get_data()
    if not data.get("new_name"):
        await state.clear()
        await show_list(target, None, sm, user, lang)
        return
    try:
        async with write_tx(sm) as s:
            b = await barbers.create_barber(
                s,
                actor_user_id=user.id,
                display_name=data["new_name"],
                bio=data.get("new_bio"),
                photo_file_id=photo,
            )
    except ServiceError as e:
        await state.clear()
        if isinstance(target, CallbackQuery):
            await views.alert(target, views.err_text(lang, e))
        else:
            await target.answer(views.err_text(lang, e))
        return
    await state.clear()
    await show_card(target, sm, user, lang, b.id, note=t(lang, "admin.br.created"))


# ------------------------------------------------------------------ edit fields


async def _start_edit(
    call: CallbackQuery, state: FSMContext, lang: str, bid: int, field: str
) -> None:
    if field not in ("name", "bio", "photo"):
        await views.stale(call, lang)
        return
    await state.clear()
    await state.set_state(AdminBarberFlow.edit_field)
    await state.update_data(barber_id=bid, field=field)
    rows = []
    if field in ("bio", "photo"):
        rows.append([btn(t(lang, "admin.br.clear_field"), acb(S, "clr", bid, field))])
    rows.append([btn(t(lang, "common.cancel"), acb(S, "pick", bid))])
    await screen.render(call, state, t(lang, f"admin.br.ask_{field}"), kb(rows))


@router.message(StateFilter(AdminBarberFlow.edit_field), F.photo)
async def on_edit_photo(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    data = await state.get_data()
    if data.get("field") != "photo":
        await message.answer(t(lang, "common.send_text"))
        return
    assert message.photo
    await _apply_edit(
        message, state, sm, user, lang, int(data["barber_id"]), "photo", message.photo[-1].file_id
    )


@router.message(StateFilter(AdminBarberFlow.edit_field), F.text)
async def on_edit_text(message: Message, state: FSMContext, sm: SM, user: Any, lang: str) -> None:
    data = await state.get_data()
    field = data.get("field")
    if field == "photo":
        await message.answer(t(lang, "admin.br.ask_photo"))
        return
    await _apply_edit(
        message,
        state,
        sm,
        user,
        lang,
        int(data["barber_id"]),
        str(field),
        (message.text or "").strip(),
    )


async def _apply_edit(
    target: Target,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
    bid: int,
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, Any] = {}
    if field == "name":
        kwargs["display_name"] = " ".join(value.split())
    elif field == "bio":
        kwargs["bio"] = value
    elif field == "photo":
        kwargs["photo_file_id"] = value
    else:
        await state.clear()
        return
    try:
        async with write_tx(sm) as s:
            await barbers.update_barber(s, actor_user_id=user.id, barber_id=bid, **kwargs)
    except ServiceError as e:
        msg = views.err_text(lang, e)
        if isinstance(target, CallbackQuery):
            await views.alert(target, msg)
        else:
            await target.answer(msg)
        return
    await state.clear()
    await show_card(target, sm, user, lang, bid, note=t(lang, "admin.saved"))


# ------------------------------------------------------------------ step / buffer


async def _step_menu(call: CallbackQuery, sm: SM, lang: str, bid: int) -> None:
    async with read_tx(sm) as s:
        b = await barbers.get_barber(s, bid)
    step_row = [
        btn(f"{'• ' if b.slot_step_min == n else ''}{n}", acb(S, "set", bid, f"st{n}"))
        for n in (15, 30, 45, 60)
    ]
    buf_row = [
        btn(f"{'• ' if b.buffer_min == n else ''}{n}", acb(S, "set", bid, f"bf{n}"))
        for n in (0, 15, 30)
    ]
    text = t(lang, "admin.br.step_menu", step=b.slot_step_min, buffer=b.buffer_min)
    from app.bot.keyboards.common import noop_btn

    rows = [
        [noop_btn(t(lang, "admin.br.step_label"))],
        step_row,
        [noop_btn(t(lang, "admin.br.buffer_label"))],
        buf_row,
    ]
    rows.append([back_btn(lang, S, "pick", bid)])
    await screen.render(call, None, text, kb(rows))


async def _set_step(call: CallbackQuery, sm: SM, user: Any, lang: str, bid: int, v: str) -> None:
    kind, num = v[:2], v[2:]
    if kind not in ("st", "bf") or not num.isdigit():
        await views.stale(call, lang)
        return
    n = int(num)
    async with write_tx(sm) as s:
        if kind == "st":
            await barbers.update_barber(s, actor_user_id=user.id, barber_id=bid, slot_step_min=n)
        else:
            await barbers.update_barber(s, actor_user_id=user.id, barber_id=bid, buffer_min=n)
    await _step_menu(call, sm, lang, bid)


# ------------------------------------------------------------------ services


async def _services(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, lang: str, a: str, bid: int, v: str
) -> None:
    async with read_tx(sm) as s:
        all_services = await catalog.list_services(s, active_only=True)
        offered = await catalog.list_barber_services(s, bid)
    data = await state.get_data()
    if a == "svc" or data.get("svc_barber") != bid:
        selected = sorted({x.service_id for x in offered if x.service_id})
        await state.clear()
        await state.update_data(svc_barber=bid, svc_sel=selected)
    else:
        selected = list(data.get("svc_sel") or [])
    if a == "svct":
        if not v.isdigit():
            await views.stale(call, lang)
            return
        sid = int(v)
        if sid in selected:
            selected.remove(sid)
        else:
            selected.append(sid)
        await state.update_data(svc_sel=selected)
    elif a == "svcs":
        if not selected:
            await views.alert(call, t(lang, "admin.br.services_none"))
            return
        async with write_tx(sm) as s:
            await catalog.set_barber_services(
                s, actor_user_id=user.id, barber_id=bid, service_ids=sorted(selected)
            )
        await state.clear()
        await show_card(call, sm, user, lang, bid, note=t(lang, "admin.saved"))
        return
    buttons = [
        btn(f"{onoff(x.id in selected)} {x.name}", acb(S, "svct", bid, str(x.id)))
        for x in all_services
    ]
    rows = chunk(buttons, 1)
    rows.append([btn(t(lang, "common.save"), acb(S, "svcs", bid)), back_btn(lang, S, "pick", bid)])
    await screen.render(call, None, t(lang, "admin.br.services_menu"), kb(rows))


# ------------------------------------------------------------------ deactivate [B6]


async def _deactivate(
    call: CallbackQuery, state: FSMContext, sm: SM, user: Any, lang: str, bid: int, v: str
) -> None:
    if v != "go":
        async with read_tx(sm) as s:
            future = await barbers.preview_future_bookings(s, actor_user_id=user.id, barber_id=bid)
        lines = [t(lang, "admin.br.deact_preview", n=len(future))]
        for b in future[:30]:
            lines.append(f"• {fmt_dt_short(b.start_at, lang)} {h(b.client_name)}")
        markup = kb(
            [
                [btn(t(lang, "admin.br.deact_go"), acb(S, "deact", bid, "go"))],
                [back_btn(lang, S, "pick", bid)],
            ]
        )
        await screen.render(call, None, "\n".join(lines), markup)
        return
    await state.clear()
    await state.set_state(AdminBarberFlow.deact_reason)
    await state.update_data(barber_id=bid)
    await screen.render(call, state, t(lang, "admin.br.deact_reason"), input_cancel_kb(lang))


@router.message(StateFilter(AdminBarberFlow.deact_reason), F.text)
async def on_deact_reason(
    message: Message, state: FSMContext, sm: SM, notifier: Notifier, user: Any, lang: str
) -> None:
    data = await state.get_data()
    reason = (message.text or "").strip()[:200]
    if not reason:
        await message.answer(t(lang, "admin.br.deact_reason"))
        return
    bid = int(data["barber_id"])
    try:
        async with write_tx(sm) as s:
            cancelled = await barbers.deactivate_barber(
                s, actor_user_id=user.id, barber_id=bid, reason=reason
            )
    except ServiceError as e:
        await state.clear()
        await message.answer(views.err_text(lang, e))
        return
    await state.clear()
    await show_card(
        message, sm, user, lang, bid, note=t(lang, "admin.br.deactivated", n=len(cancelled))
    )
    await notifier.time_off_cancellations(cancelled)


@router.message(
    StateFilter(
        AdminBarberFlow.name,
        AdminBarberFlow.bio,
        AdminBarberFlow.photo,
        AdminBarberFlow.edit_field,
        AdminBarberFlow.deact_reason,
    )
)
async def on_unexpected(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.send_text"))
