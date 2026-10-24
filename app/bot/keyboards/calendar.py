"""Inline month calendar (own implementation, [#3] P2)."""

from __future__ import annotations

import calendar as _cal
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import CalCb
from app.bot.i18n import month_name, weekday_short
from app.bot.keyboards.common import btn, kb, noop_btn, wiz_nav_row


def ym(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}"


def parse_ym(v: str) -> tuple[int, int] | None:
    try:
        y, m = int(v[:4]), int(v[4:6])
    except ValueError:
        return None
    if not (1 <= m <= 12) or len(v) != 6:
        return None
    return y, m


def _shift(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def month_kb(
    year: int,
    month: int,
    *,
    available: set[date],
    first: date,
    last: date,
    lang: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    py, pm = _shift(year, month, -1)
    ny, nm = _shift(year, month, 1)
    has_prev = (py, pm) >= (first.year, first.month)
    has_next = (ny, nm) <= (last.year, last.month)
    rows.append(
        [
            btn("◀️", CalCb(a="m", v=f"{py:04d}{pm:02d}")) if has_prev else noop_btn(" "),
            noop_btn(f"{month_name(month, lang).capitalize()} {year}"),
            btn("▶️", CalCb(a="m", v=f"{ny:04d}{nm:02d}")) if has_next else noop_btn(" "),
        ]
    )
    rows.append([noop_btn(weekday_short(i, lang)) for i in range(7)])
    for week in _cal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            if d.month != month:
                row.append(noop_btn(" "))
            elif d in available:
                row.append(btn(str(d.day), CalCb(a="d", v=d.strftime("%Y%m%d"))))
            elif first <= d <= last:
                row.append(noop_btn("·"))
            else:
                row.append(noop_btn(" "))
        rows.append(row)
    rows.append(wiz_nav_row(lang))
    return kb(rows)
