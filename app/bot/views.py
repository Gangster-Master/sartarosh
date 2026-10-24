"""Shared screens and text builders used by several routers and the notifier."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen
from app.bot.callbacks import ConsentCb, HubCb
from app.bot.i18n import fmt_dt, fmt_price, h, t
from app.bot.keyboards.common import btn, kb, lang_picker, menu_for
from app.db.enums import Awaiting, BookingSource, BookingStatus
from app.db.uow import read_tx
from app.services import bookings, settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

STATUS_ICON = {
    BookingStatus.PENDING: "⏳",
    BookingStatus.CONFIRMED: "✅",
    BookingStatus.COMPLETED: "✔️",
    BookingStatus.NO_SHOW: "🚫",
    BookingStatus.REJECTED: "❌",
    BookingStatus.EXPIRED: "⌛",
    BookingStatus.CANCELLED_BY_CLIENT: "↩️",
    BookingStatus.CANCELLED_BY_BARBER: "✖️",
}

REASON_CODES = ("busy", "sick")


# ----------------------------------------------------------------- parsing helpers


def day_code(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_day(v: str | None) -> date | None:
    if not v or len(v) != 8 or not v.isdigit():
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    except ValueError:
        return None


def hhmm(tm: time) -> str:
    return f"{tm.hour:02d}{tm.minute:02d}"


def parse_hhmm(v: str | None) -> time | None:
    if not v or len(v) != 4 or not v.isdigit():
        return None
    hh, mm = int(v[:2]), int(v[2:])
    if hh > 23 or mm > 59:
        return None
    return time(hh, mm)


def parse_time_text(raw: str) -> time | None:
    """'9', '9:00', '09.30', '0930' → time."""
    s = raw.strip().replace(".", ":").replace(" ", "")
    if ":" in s:
        a, _, b = s.partition(":")
    elif len(s) in (3, 4) and s.isdigit():
        a, b = s[:-2], s[-2:]
    else:
        a, b = s, "0"
    if not (a.isdigit() and b.isdigit()):
        return None
    hh, mm = int(a), int(b)
    if hh == 24 and mm == 0:
        return None
    if hh > 23 or mm > 59:
        return None
    return time(hh, mm)


def parse_time_range(raw: str) -> tuple[time, time] | None:
    """'10:00-20:00' (also with – or —) → (start, end) with start < end."""
    s = raw.replace("–", "-").replace("—", "-")
    if "-" not in s:
        return None
    a, _, b = s.partition("-")
    ta, tb = parse_time_text(a), parse_time_text(b)
    if ta is None or tb is None or ta >= tb:
        return None
    return ta, tb


def parse_date_text(raw: str, today: date) -> date | None:
    """'26.10', '26.10.2026', '2026-10-26' → date (a past 'DD.MM' rolls to next year)."""
    s = raw.strip()
    try:
        if "-" in s and len(s) == 10:
            return date.fromisoformat(s)
        parts = s.replace("/", ".").split(".")
        if len(parts) == 2:
            d = date(today.year, int(parts[1]), int(parts[0]))
            if d < today:
                d = date(today.year + 1, d.month, d.day)
            return d
        if len(parts) == 3:
            y = int(parts[2])
            if y < 100:
                y += 2000
            return date(y, int(parts[1]), int(parts[0]))
    except ValueError:
        return None
    return None


# ----------------------------------------------------------------- errors


def err_text(lang: str, e: ServiceError) -> str:
    """``err.<code>.<reason|kind|field>`` when such a key exists, else ``err.<code>``."""
    from app.bot.i18n import has_key

    ctx = dict(getattr(e, "ctx", {}) or {})
    code = getattr(e, "code", "error")
    for attr in ("reason", "kind", "field"):
        val = ctx.get(attr)
        if val:
            specific = f"err.{code}.{val}"
            if has_key(specific):
                return t(lang, specific, **ctx)
    key = f"err.{code}"
    if has_key(key):
        return t(lang, key, **ctx)
    return t(lang, "err.error")


async def alert(call: CallbackQuery, text: str) -> None:
    await screen.safe_answer(call, text[:200], alert=True)


async def stale(call: CallbackQuery, lang: str) -> None:
    await screen.safe_answer(call, t(lang, "common.stale"), alert=True)


# ----------------------------------------------------------------- booking texts


def status_label(lang: str, b: Any) -> str:
    st = b.status
    if st == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT:
        return t(lang, "status.PENDING_CLIENT")
    return t(lang, f"status.{st.value}")


def reason_label(lang: str, reason: str | None) -> str:
    if not reason:
        return ""
    if reason in REASON_CODES or reason in (
        "blocked",
        "deleted",
        "proposal_declined",
        "withdrawn",
        "time_off",
        "barber_inactive",
    ):
        return t(lang, f"reason.{reason}")
    return h(reason)


def booking_lines(b: Any, lang: str, *, staff: bool, show_status: bool = True) -> list[str]:
    lines: list[str] = []
    if show_status:
        lines.append(f"{STATUS_ICON.get(b.status, '•')} <b>{status_label(lang, b)}</b> · #{b.id}")
    lines.append(t(lang, "card.when", when=fmt_dt(b.start_at, lang)))
    lines.append(t(lang, "card.barber", name=h(b.barber_name)))
    if b.service_name:
        lines.append(t(lang, "card.service", name=h(b.service_name), min=b.duration_min))
    if b.price_snapshot or b.final_price_uzs:
        if b.bonus_cost > 0 and b.final_price_uzs != b.price_snapshot:
            lines.append(
                t(
                    lang,
                    "card.price_bonus",
                    price=fmt_price(b.final_price_uzs, lang),
                    full=fmt_price(b.price_snapshot, lang),
                )
            )
        else:
            lines.append(t(lang, "card.price", price=fmt_price(b.final_price_uzs, lang)))
    if staff:
        phone = h(b.client_phone) if b.client_phone else "—"
        lines.append(t(lang, "card.client", name=h(b.client_name), phone=phone))
        if b.source == BookingSource.MANUAL:
            lines.append(t(lang, "card.manual"))
    if b.comment:
        lines.append(t(lang, "card.comment", text=h(b.comment)))
    if b.original_start_at and b.status == BookingStatus.PENDING:
        lines.append(t(lang, "card.original", when=fmt_dt(b.original_start_at, lang)))
    if b.cancel_reason and b.status in (
        BookingStatus.REJECTED,
        BookingStatus.CANCELLED_BY_BARBER,
        BookingStatus.CANCELLED_BY_CLIENT,
    ):
        lines.append(t(lang, "card.reason", reason=reason_label(lang, b.cancel_reason)))
    return lines


def booking_text(b: Any, lang: str, *, staff: bool, title: str | None = None) -> str:
    lines = booking_lines(b, lang, staff=staff)
    if title:
        lines.insert(0, title)
    return "\n".join(lines)


# ----------------------------------------------------------------- onboarding / hub


def consent_kb(lang: str) -> InlineKeyboardMarkup:
    return kb([[btn(t(lang, "onb.agree"), ConsentCb(a="ok"))]])


def needs_lang_picker(user: Any, language_code: str | None) -> bool:
    code = (language_code or "").lower()
    return not user.lang_chosen and not code.startswith(("uz", "ru"))


async def send_onboarding(
    bot: Bot, chat_id: int, user: Any, lang: str, language_code: str | None
) -> None:
    try:
        if needs_lang_picker(user, language_code):
            await bot.send_message(chat_id, t(lang, "onb.choose_lang"), reply_markup=lang_picker())
        else:
            await bot.send_message(chat_id, t(lang, "onb.consent"), reply_markup=consent_kb(lang))
    except TelegramAPIError as e:
        log.debug("send_onboarding failed: %s", e)


async def hub_content(
    sm: async_sessionmaker[AsyncSession], user: Any, roles: Any, lang: str
) -> tuple[str, InlineKeyboardMarkup]:
    async with read_tx(sm) as s:
        shop = await settings.get_shop_settings(s)
        hint = await bookings.get_last_completed(s, client_user_id=user.id)
    name = h(user.display_name or user.first_name or "")
    shop_name = h(shop.shop_name) if shop.shop_name else t(lang, "hub.default_shop")
    text = t(lang, "hub.text", name=name, shop=shop_name)
    rows = []
    if hint is not None:
        label = hint.barber_name  # button text is plain, not HTML
        if hint.service_name:
            label = f"{label}, {hint.service_name}"
        rows.append([btn(t(lang, "hub.rebook", what=label), HubCb(a="rebook"))])
    rows.append([btn(t(lang, "hub.book"), HubCb(a="book"))])
    rows.append(
        [btn(t(lang, "hub.my"), HubCb(a="my")), btn(t(lang, "hub.bonus"), HubCb(a="bonus"))]
    )
    rows.append(
        [btn(t(lang, "hub.prices"), HubCb(a="prices")), btn(t(lang, "hub.info"), HubCb(a="info"))]
    )
    extra = []
    if shop.referral_enabled and shop.loyalty_enabled:
        extra.append(btn(t(lang, "hub.ref"), HubCb(a="ref")))
    extra.append(btn(t(lang, "hub.lang"), HubCb(a="lang")))
    rows.append(extra)
    return text, kb(rows)


async def show_hub(
    target: CallbackQuery | Message,
    state: FSMContext | None,
    sm: async_sessionmaker[AsyncSession],
    user: Any,
    roles: Any,
    lang: str,
    *,
    prefix: str | None = None,
    new_message: bool = False,
) -> int:
    text, markup = await hub_content(sm, user, roles, lang)
    if prefix:
        text = f"{prefix}\n\n{text}"
    return await screen.render(target, state, text, markup, new_message=new_message)


async def send_home(
    message: Message,
    state: FSMContext,
    sm: async_sessionmaker[AsyncSession],
    user: Any,
    roles: Any,
    lang: str,
    *,
    view: str | None = None,
    prefix: str | None = None,
) -> None:
    """Fresh reply keyboard for the role view + (for clients) a fresh hub message."""
    from app.bot.keyboards.common import default_view

    view = view or default_view(roles)
    bot: Bot = message.bot  # type: ignore[assignment]
    head = prefix or t(lang, f"home.{view}")
    await screen.send_reply_kb(bot, message.chat.id, head, menu_for(roles, lang, view))
    if view == "c":
        await show_hub(message, state, sm, user, roles, lang, new_message=True)


def now_local_date() -> date:
    from app.services import clock

    return clock.local_today()


def aware_now() -> datetime:
    from app.services import clock

    return clock.now_utc()
