"""Shared keyboards: role reply menus, contact/location requests, nav rows."""

from __future__ import annotations

from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot.callbacks import NOOP, HubCb, LangCb, RoleCb, WizCb
from app.bot.i18n import t

# keys of every reply-keyboard label, per role view
CLIENT_MENU_KEYS = ("menu.book", "menu.my", "menu.bonus", "menu.prices", "menu.info")
BARBER_MENU_KEYS = (
    "menu.b_today",
    "menu.b_upcoming",
    "menu.b_busy",
    "menu.b_block",
    "menu.b_manual",
    "menu.b_client",
)
ADMIN_MENU_KEYS = (
    "menu.a_barbers",
    "menu.a_services",
    "menu.a_hours",
    "menu.a_timeoff",
    "menu.a_bookings",
    "menu.a_clients",
    "menu.a_stats",
    "menu.a_settings",
    "menu.a_admins",
    "menu.role",
)
ALL_MENU_KEYS = tuple(dict.fromkeys(CLIENT_MENU_KEYS + BARBER_MENU_KEYS + ADMIN_MENU_KEYS))


def btn(text: str, cb: Any) -> InlineKeyboardButton:
    data = cb if isinstance(cb, str) else cb.pack()
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def noop_btn(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=NOOP)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def chunk(items: list[InlineKeyboardButton], n: int) -> list[list[InlineKeyboardButton]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _rk(rows: list[list[str]], *, placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def default_view(roles: Any) -> str:
    if roles is not None and roles.is_admin:
        return "a"
    if roles is not None and roles.is_barber and roles.barber_id:
        return "b"
    return "c"


def client_menu(lang: str, roles: Any = None) -> ReplyKeyboardMarkup:
    rows = [
        [t(lang, "menu.book")],
        [t(lang, "menu.my"), t(lang, "menu.bonus")],
        [t(lang, "menu.prices"), t(lang, "menu.info")],
    ]
    if roles is not None and (roles.is_admin or (roles.is_barber and roles.barber_id)):
        rows.append([t(lang, "menu.role")])
    return _rk(rows)


def barber_menu(lang: str, roles: Any = None) -> ReplyKeyboardMarkup:
    rows = [
        [t(lang, "menu.b_today"), t(lang, "menu.b_upcoming")],
        [t(lang, "menu.b_busy"), t(lang, "menu.b_block")],
        [t(lang, "menu.b_manual"), t(lang, "menu.b_client")],
    ]
    if roles is not None and roles.is_admin:
        rows.append([t(lang, "menu.role")])
    return _rk(rows)


def admin_menu(lang: str, roles: Any = None) -> ReplyKeyboardMarkup:
    return _rk(
        [
            [t(lang, "menu.a_barbers"), t(lang, "menu.a_services")],
            [t(lang, "menu.a_hours"), t(lang, "menu.a_timeoff")],
            [t(lang, "menu.a_bookings"), t(lang, "menu.a_clients")],
            [t(lang, "menu.a_stats"), t(lang, "menu.a_settings")],
            [t(lang, "menu.a_admins"), t(lang, "menu.role")],
        ]
    )


def menu_for(roles: Any, lang: str, view: str | None = None) -> ReplyKeyboardMarkup:
    view = view or default_view(roles)
    if view == "a" and roles is not None and roles.is_admin:
        return admin_menu(lang, roles)
    if view == "b" and roles is not None and roles.is_barber and roles.barber_id:
        return barber_menu(lang, roles)
    return client_menu(lang, roles)


def role_picker(lang: str, roles: Any) -> InlineKeyboardMarkup:
    rows = [[btn(t(lang, "role.client"), RoleCb(r="c"))]]
    if roles.is_barber and roles.barber_id:
        rows.append([btn(t(lang, "role.barber"), RoleCb(r="b"))])
    if roles.is_admin:
        rows.append([btn(t(lang, "role.admin"), RoleCb(r="a"))])
    return kb(rows)


def contact_request(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(lang, "contact.button"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_request(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "admin.st.loc_button"), request_location=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def lang_picker() -> InlineKeyboardMarkup:
    return kb([[btn("🇺🇿 O'zbekcha", LangCb(l="uz")), btn("🇷🇺 Русский", LangCb(l="ru"))]])


def home_btn(lang: str) -> InlineKeyboardButton:
    return btn(t(lang, "common.home"), HubCb(a="home"))


def wiz_nav_row(lang: str, *, back: bool = True) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(btn(t(lang, "common.back"), WizCb(a="back")))
    row.append(btn(t(lang, "common.cancel"), WizCb(a="cancel")))
    return row


def cancel_only(lang: str, cb: Any) -> InlineKeyboardMarkup:
    return kb([[btn(t(lang, "common.cancel"), cb)]])
