"""Hub buttons, prices & barber cards, address & hours, bonus page, referral."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import BarberCardCb, HubCb
from app.bot.i18n import fmt_date_short, fmt_price, fmt_stamps, h, t, weekday_name, weekday_short
from app.bot.keyboards.common import btn, home_btn, kb, lang_picker, url_btn
from app.db.enums import BonusReason, LoyaltyMode
from app.db.uow import read_tx
from app.services import barbers, bonus, bookings, catalog, clock, schedule, settings, validation
from app.services.errors import NotFoundError

log = logging.getLogger(__name__)

router = Router(name="client_info")
router.message.filter(F.chat.type == "private")

SM = async_sessionmaker[AsyncSession]
ANY = StateFilter("*")
Target = CallbackQuery | Message


# ------------------------------------------------------------------ hub buttons


@router.callback_query(HubCb.filter(), ANY)
async def on_hub(
    call: CallbackQuery,
    callback_data: HubCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    from app.bot.handlers import client_booking, client_my

    a = callback_data.a
    await state.clear()
    if a == "book":
        await client_booking.start_booking(call, state, sm, user, lang, entry="service_first")
    elif a == "rebook":
        async with read_tx(sm) as s:
            hint = await bookings.get_last_completed(s, client_user_id=user.id)
        if hint is None:
            await client_booking.start_booking(call, state, sm, user, lang, entry="service_first")
        else:
            await client_booking.start_booking(
                call,
                state,
                sm,
                user,
                lang,
                entry="rebook",
                barber_id=hint.barber_id,
                service_id=hint.service_id,
            )
    elif a == "my":
        await client_my.show_my(call, state, sm, user, lang)
    elif a == "bonus":
        await show_bonus(call, state, sm, user, lang)
    elif a == "info":
        await show_address(call, state, sm, lang)
    elif a == "prices":
        await show_prices(call, state, sm, lang)
    elif a == "ref":
        await show_referral(call, state, sm, user, lang)
    elif a == "lang":
        await screen.render(call, None, t(lang, "onb.choose_lang"), lang_picker())
    else:  # home
        await views.show_hub(call, state, sm, user, roles, lang)


# ------------------------------------------------------------------ prices & barbers [#58][#57]


async def show_prices(target: Target, state: FSMContext | None, sm: SM, lang: str) -> None:
    async with read_tx(sm) as s:
        services = await catalog.list_services(s, active_only=True)
        team = await barbers.list_barbers(s, public_only=True)
    lines = [t(lang, "info.prices_title"), ""]
    if services:
        for x in services:
            lines.append(
                t(
                    lang,
                    "info.price_line",
                    name=h(x.name),
                    price=fmt_price(x.price_uzs, lang),
                    min=x.duration_min,
                )
            )
    else:
        lines.append(t(lang, "info.no_services"))
    rows = []
    if team:
        lines.extend(["", t(lang, "info.team")])
        rows = [[btn(f"💈 {b.display_name}", BarberCardCb(a="view", b=b.id))] for b in team]
    rows.append([btn(t(lang, "hub.book"), HubCb(a="book")), home_btn(lang)])
    await screen.render(target, state, "\n".join(lines), kb(rows))


async def barber_card_content(sm: SM, barber_id: int, lang: str) -> tuple[str, str | None]:
    async with read_tx(sm) as s:
        b = await barbers.get_barber(s, barber_id)
        offered = await catalog.list_barber_services(s, barber_id) if b.is_public else []
    lines = [f"💈 <b>{h(b.display_name)}</b>"]
    if b.bio:
        lines.extend(["", h(b.bio)])
    if offered:
        lines.extend(["", t(lang, "info.barber_services")])
        for rs in offered:
            if rs.name:
                lines.append(
                    t(
                        lang,
                        "info.price_line",
                        name=h(rs.name),
                        price=fmt_price(rs.price_uzs, lang),
                        min=rs.duration_min,
                    )
                )
    if not b.is_public:
        lines.extend(["", t(lang, "info.barber_unavailable")])
    return "\n".join(lines), b.photo_file_id


@router.callback_query(BarberCardCb.filter(), ANY)
async def on_barber_card(
    call: CallbackQuery,
    callback_data: BarberCardCb,
    state: FSMContext,
    sm: SM,
    user: Any,
    lang: str,
) -> None:
    from app.bot.handlers import client_booking

    if callback_data.a == "back":
        await state.clear()
        await show_prices(call, state, sm, lang)
        return
    try:
        async with read_tx(sm) as s:
            b = await barbers.get_barber(s, callback_data.b)
    except NotFoundError:
        await views.stale(call, lang)
        return
    if callback_data.a == "book":
        if not b.is_public:
            await views.alert(call, t(lang, "err.barber_inactive"))
            return
        await client_booking.start_booking(
            call, state, sm, user, lang, entry="barber_first", barber_id=b.id
        )
        return
    await state.clear()
    text, photo = await barber_card_content(sm, b.id, lang)
    rows = []
    if b.is_public:
        rows.append([btn(t(lang, "info.book_this"), BarberCardCb(a="book", b=b.id))])
    rows.append([btn(t(lang, "common.back"), BarberCardCb(a="back", b=b.id))])
    await screen.render(call, state, text, kb(rows), photo=photo)


# ------------------------------------------------------------------ address & hours [#59][#60]


def _hours_line(day: Any, lang: str) -> str:
    if day.open_time is None or day.close_time is None:
        return t(lang, "info.closed")
    return f"{day.open_time.strftime('%H:%M')}–{day.close_time.strftime('%H:%M')}"


async def show_address(target: Target, state: FSMContext | None, sm: SM, lang: str) -> None:
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
        week = await schedule.get_shop_week_hours(s)
    now_local = clock.to_local(clock.now_utc())
    lines = []
    lines.append(
        f"💈 <b>{h(shop.shop_name)}</b>" if shop.shop_name else t(lang, "info.address_title")
    )
    if shop.address:
        lines.append(t(lang, "notify.address", address=h(shop.address)))
    if shop.contact_phone:
        lines.append(t(lang, "notify.contact", phone=h(shop.contact_phone)))
    by_wd = {d.weekday: d for d in week}
    today = by_wd.get(now_local.weekday())
    if today is not None:
        is_open = (
            today.open_time is not None
            and today.close_time is not None
            and today.open_time <= now_local.time() < today.close_time
        )
        status = t(lang, "info.open_now") if is_open else t(lang, "info.closed_now")
        lines.extend(
            ["", t(lang, "info.today_hours", hours=_hours_line(today, lang), status=status)]
        )
    lines.extend(["", t(lang, "info.week_hours")])
    for wd in range(7):
        d = by_wd.get(wd)
        lines.append(
            f"{weekday_name(wd, lang).capitalize()}: {_hours_line(d, lang) if d else t(lang, 'info.closed')}"
        )
    maps = []
    for key, label in (
        ("map_yandex_url", "Yandex"),
        ("map_google_url", "Google"),
        ("map_2gis_url", "2GIS"),
    ):
        url = getattr(shop, key, "") or ""
        if validation.is_web_url(url):
            maps.append(url_btn(f"🗺 {label}", url))
    nav = [btn(t(lang, "hub.book"), HubCb(a="book")), home_btn(lang)]
    try:
        await screen.render(target, state, "\n".join(lines), kb([maps, nav] if maps else [nav]))
    except TelegramBadRequest as e:  # a map URL Telegram still rejects: show the page without it
        log.warning("address page: map buttons rejected: %s", e)
        await screen.render(target, state, "\n".join(lines), kb([nav]))
    if shop.latitude is not None and shop.longitude is not None:
        bot: Bot = target.bot  # type: ignore[assignment]
        chat_id = target.chat.id if isinstance(target, Message) else target.from_user.id
        try:
            await bot.send_venue(
                chat_id=chat_id,
                latitude=float(shop.latitude),
                longitude=float(shop.longitude),
                title=shop.shop_name or "💈",
                address=shop.address or " ",
            )
        except TelegramAPIError as e:
            log.debug("send_venue failed: %s", e)


# ------------------------------------------------------------------ bonus [#43]


def _reason_label(lang: str, reason: BonusReason) -> str:
    return t(lang, f"bonus.reason.{reason.value}")


def _tx_line(tx: Any, lang: str, mode: LoyaltyMode) -> str:
    created: datetime = tx.created_at
    loc = clock.to_local(created)
    amount = (
        f"{tx.delta:+d}"
        if tx.unit == LoyaltyMode.STAMPS
        else f"{'+' if tx.delta > 0 else '−'}{fmt_price(abs(tx.delta), lang)}"
    )
    comment = f" · {h(tx.comment)}" if tx.comment else ""
    return f"{weekday_short(loc.weekday(), lang)} {fmt_date_short(loc.date())} · {amount} · {_reason_label(lang, tx.reason)}{comment}"


async def show_bonus(
    target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str
) -> None:
    async with read_tx(sm) as s:
        st = await bonus.get_bonus_status(s, user.id)
        shop = await settings.get_shop_settings(s)
    lines = [t(lang, "bonus.title"), ""]
    rows = []
    if not st.enabled:
        lines.append(t(lang, "bonus.disabled"))
    elif st.mode == LoyaltyMode.STAMPS:
        lines.append(f"{fmt_stamps(st.progress, st.required)}  <b>{st.progress}/{st.required}</b>")
        if st.reward_type == "free":
            reward = t(lang, "bonus.reward_free", n=st.required + 1)
        else:
            reward = t(lang, "bonus.reward_percent", n=st.required + 1, value=st.reward_value)
        if st.can_redeem:
            lines.append(t(lang, "bonus.ready"))
        else:
            lines.append(
                t(lang, "bonus.left", left=max(0, st.required - st.progress), reward=reward)
            )
        if st.reserved:
            lines.append(t(lang, "bonus.reserved", n=st.reserved))
    else:
        lines.append(t(lang, "bonus.cash_balance", amount=fmt_price(st.available, lang)))
        lines.append(
            t(lang, "bonus.cash_rules", percent=st.cashback_percent, max=st.max_redeem_percent)
        )
        if st.reserved:
            lines.append(t(lang, "bonus.cash_reserved", amount=fmt_price(st.reserved, lang)))
    if st.recent:
        lines.extend(["", t(lang, "bonus.history")])
        lines.extend(_tx_line(tx, lang, st.mode) for tx in st.recent)
    if st.enabled and shop.referral_enabled:
        rows.append([btn(t(lang, "hub.ref"), HubCb(a="ref"))])
    rows.append([btn(t(lang, "hub.book"), HubCb(a="book")), home_btn(lang)])
    await screen.render(target, state, "\n".join(lines), kb(rows))


# ------------------------------------------------------------------ referral [#46] (P2)


async def show_referral(
    target: Target, state: FSMContext | None, sm: SM, user: Any, lang: str
) -> None:
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
    bot: Bot = target.bot  # type: ignore[assignment]
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{user.ref_code}"
    if not (shop.referral_enabled and shop.loyalty_enabled):
        await screen.render(target, state, t(lang, "ref.disabled"), kb([[home_btn(lang)]]))
        return
    text = t(lang, "ref.text", link=h(link), n=shop.referral_bonus)
    share_text = t(lang, "ref.share_text", shop=shop.shop_name or "")
    share = f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"
    await screen.render(
        target, state, text, kb([[url_btn(t(lang, "ref.share_btn"), share)], [home_btn(lang)]])
    )
