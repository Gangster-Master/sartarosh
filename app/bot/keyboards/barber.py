"""Barber-side inline keyboards."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot.callbacks import BookingCb, BusyCb, ProposeCb, QueueDayCb, RejectCb
from app.bot.i18n import fmt_day_btn, fmt_time, t
from app.bot.keyboards.common import btn, chunk, kb
from app.services import clock


def decision_kb(booking_id: int, lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "barber.accept"), BookingCb(a="acc", id=booking_id)),
                btn(t(lang, "barber.reject"), BookingCb(a="rej", id=booking_id)),
            ],
            [btn(t(lang, "barber.propose"), BookingCb(a="prop", id=booking_id))],
        ]
    )


def reject_reasons_kb(booking_id: int, lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "reason.busy"), RejectCb(id=booking_id, r="busy")),
                btn(t(lang, "reason.sick"), RejectCb(id=booking_id, r="sick")),
            ],
            [
                btn(t(lang, "barber.reason_other"), RejectCb(id=booking_id, r="other")),
                btn(t(lang, "barber.reason_none"), RejectCb(id=booking_id, r="none")),
            ],
            [btn(t(lang, "common.back"), BookingCb(a="dback", id=booking_id))],
        ]
    )


def proposal_kb(booking_id: int, slots: list[datetime], lang: str) -> InlineKeyboardMarkup:
    today = clock.local_today()
    buttons = []
    for s in slots:
        loc = clock.to_local(s)
        label = f"{fmt_day_btn(loc.date(), today, lang)} {fmt_time(s)}"
        buttons.append(
            btn(label, ProposeCb(id=booking_id, d=loc.strftime("%Y%m%d"), t=loc.strftime("%H%M")))
        )
    rows = chunk(buttons, 2)
    rows.append([btn(t(lang, "common.back"), BookingCb(a="dback", id=booking_id))])
    return kb(rows)


def queue_kb(
    items: list[Any], day: date, lang: str, *, with_days: bool = True
) -> InlineKeyboardMarkup:
    from app.bot.views import STATUS_ICON

    rows = []
    for b in items:
        name = b.client_name if len(b.client_name) <= 18 else b.client_name[:17] + "…"
        rows.append(
            [
                btn(
                    f"{STATUS_ICON.get(b.status, '•')} {fmt_time(b.start_at)} {name}",
                    BookingCb(a="card", id=b.id),
                )
            ]
        )
    nav = [btn(t(lang, "common.refresh"), QueueDayCb(d=day.strftime("%Y%m%d")))]
    if with_days:
        nav.append(btn(t(lang, "barber.other_days"), QueueDayCb(d="")))
    rows.append(nav)
    return kb(rows)


def days_kb(days: list[date], lang: str) -> InlineKeyboardMarkup:
    today = clock.local_today()
    buttons = [btn(fmt_day_btn(d, today, lang), QueueDayCb(d=d.strftime("%Y%m%d"))) for d in days]
    return kb(chunk(buttons, 3))


def busy_kb(lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(t(lang, "barber.busy30"), BusyCb(m=30)),
                btn(t(lang, "barber.busy60"), BusyCb(m=60)),
            ]
        ]
    )
