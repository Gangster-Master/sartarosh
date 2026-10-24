"""Admin keyboards and small helpers."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import AdminCb, HubCb
from app.bot.i18n import t
from app.bot.keyboards.common import btn, kb


def acb(s: str, a: str, id: int = 0, v: str = "") -> AdminCb:  # noqa: A002 - contract field name
    return AdminCb(s=s, a=a, id=id, v=v)


def back_btn(lang: str, s: str, a: str = "list", id: int = 0, v: str = "") -> InlineKeyboardButton:  # noqa: A002
    return btn(t(lang, "common.back"), acb(s, a, id, v))


def input_cancel_kb(lang: str) -> InlineKeyboardMarkup:
    """Inline ❌ under every admin text prompt (clears the FSM) [E1]."""
    return kb([[btn(t(lang, "common.cancel"), HubCb(a="cx"))]])


def yes_no_kb(lang: str, yes: AdminCb, no: AdminCb) -> InlineKeyboardMarkup:
    return kb([[btn(t(lang, "common.yes"), yes), btn(t(lang, "common.no"), no)]])


def onoff(value: bool) -> str:
    return "✅" if value else "▫️"
