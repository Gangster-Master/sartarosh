"""Client-side inline keyboards."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import (
    BarberCardCb,
    BarberCb,
    BookingCb,
    CalCb,
    DayCb,
    HubCb,
    RateCb,
    RebookCb,
    SlotCb,
    SvcCb,
    WizCb,
)
from app.bot.i18n import fmt_day_btn, fmt_price, t
from app.bot.keyboards.common import btn, chunk, home_btn, kb, noop_btn, wiz_nav_row
from app.services import clock

QUICK_DAYS = 7


def service_label(name: str, price: int, minutes: int, lang: str) -> str:
    return f"{name} — {fmt_price(price, lang)} · {t(lang, 'common.minutes', n=minutes)}"


def services_kb(items: list[tuple[int, str]], lang: str, *, back: bool) -> InlineKeyboardMarkup:
    rows = [[btn(label, SvcCb(s=sid))] for sid, label in items]
    rows.append(wiz_nav_row(lang, back=back))
    return kb(rows)


def rating_badge(b: Any) -> str:
    """Short public rating suffix for a barber button: ' ⭐4.8 (12)'."""
    if getattr(b, "rating_count", 0):
        return f" ⭐{b.rating_avg:.1f} ({b.rating_count})"
    return ""


def barbers_kb(
    barbers: list[Any], lang: str, *, back: bool, allow_any: bool = True
) -> InlineKeyboardMarkup:
    rows = []
    if allow_any and len(barbers) > 1:
        rows.append([btn(t(lang, "book.any_barber"), BarberCb(b=0))])
    for b in barbers:
        rows.append(
            [
                btn(f"💈 {b.display_name}{rating_badge(b)}", BarberCb(b=b.id)),
                btn("ℹ️", BarberCardCb(a="view", b=b.id)),
            ]
        )
    rows.append(wiz_nav_row(lang, back=back))
    return kb(rows)


def days_kb(days: list[Any], lang: str, *, back: bool, more: bool) -> InlineKeyboardMarkup:
    """``days``: DayAvailability list (today first). Full days are shown with '· band'."""
    today = clock.local_today()
    buttons: list[InlineKeyboardButton] = []
    for d in days[:QUICK_DAYS]:
        if not d.is_working or not d.has_free_slots:
            if d.day == today:
                continue  # "Bugun" is hidden when it has no slots
            label = f"{fmt_day_btn(d.day, today, lang)} · {t(lang, 'book.day_full')}"
        else:
            label = fmt_day_btn(d.day, today, lang)
        buttons.append(btn(label, DayCb(d=d.day.strftime("%Y%m%d"))))
    rows = chunk(buttons, 3)
    if more:
        first = days[0].day if days else today
        rows.append([btn(t(lang, "book.other_day"), CalCb(a="m", v=first.strftime("%Y%m")))])
    rows.append(wiz_nav_row(lang, back=back))
    return kb(rows)


def slots_kb(
    slots: list[datetime], *, barber_id: int, service_id: int, day: date, lang: str
) -> InlineKeyboardMarkup:
    groups: dict[str, list[InlineKeyboardButton]] = {"morning": [], "day": [], "evening": []}
    dcode = day.strftime("%Y%m%d")
    for s in slots:
        loc = clock.to_local(s)
        g = "morning" if loc.hour < 12 else ("day" if loc.hour < 17 else "evening")
        groups[g].append(
            btn(
                loc.strftime("%H:%M"),
                SlotCb(b=barber_id, s=service_id, d=dcode, t=loc.strftime("%H%M")),
            )
        )
    rows: list[list[InlineKeyboardButton]] = []
    for g in ("morning", "day", "evening"):
        if groups[g]:
            rows.append([noop_btn(t(lang, f"book.group_{g}"))])
            rows.extend(chunk(groups[g], 4))
    rows.append(wiz_nav_row(lang))
    return kb(rows)


def comment_kb(lang: str) -> InlineKeyboardMarkup:
    return kb([[btn(t(lang, "book.skip_comment"), WizCb(a="skip_comment"))], wiz_nav_row(lang)])


def confirm_kb(lang: str, *, bonus_possible: bool, use_bonus: bool) -> InlineKeyboardMarkup:
    rows = [[btn(t(lang, "book.confirm_btn"), WizCb(a="confirm"))]]
    if bonus_possible:
        if use_bonus:
            rows.append([btn(t(lang, "book.bonus_off"), WizCb(a="bonus_off"))])
        else:
            rows.append([btn(t(lang, "book.bonus_on"), WizCb(a="bonus_on"))])
    rows.append([btn(t(lang, "book.edit_name"), WizCb(a="edit_name"))])
    rows.append(wiz_nav_row(lang))
    return kb(rows)


def result_kb(b: Any, lang: str) -> InlineKeyboardMarkup:
    from app.db.enums import Awaiting, BookingStatus

    rows = []
    if b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER:
        rows.append([btn(t(lang, "my.withdraw"), BookingCb(a="wd", id=b.id))])
    elif b.status == BookingStatus.CONFIRMED:
        rows.append([btn(t(lang, "my.cancel"), BookingCb(a="ccx", id=b.id))])
    rows.append([btn(t(lang, "hub.my"), HubCb(a="my")), home_btn(lang)])
    return kb(rows)


def rebook_kb(barber_id: int, service_id: int | None, lang: str) -> InlineKeyboardMarkup:
    return kb([[btn(t(lang, "notify.rebook_btn"), RebookCb(b=barber_id, s=service_id or 0))]])


def proposal_answer_kb(booking_id: int, lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "my.proposal_yes"), BookingCb(a="pya", id=booking_id)),
                btn(t(lang, "my.proposal_no"), BookingCb(a="pno", id=booking_id)),
            ]
        ]
    )


def reminder24_kb(booking_id: int, lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "notify.ack_btn"), BookingCb(a="ack", id=booking_id)),
                btn(t(lang, "my.cancel"), BookingCb(a="ccx", id=booking_id)),
            ]
        ]
    )


def visit_confirm_kb(booking_id: int, lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "common.yes"), BookingCb(a="vy", id=booking_id)),
                btn(t(lang, "common.no"), BookingCb(a="vn", id=booking_id)),
            ]
        ]
    )


def rating_kb(booking_id: int) -> InlineKeyboardMarkup:
    return kb([[btn(f"⭐{v}", RateCb(id=booking_id, v=v)) for v in range(1, 6)]])
