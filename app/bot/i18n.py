"""Localisation helpers (ARCHITECTURE.md §6.6).

``t(lang, key, **kw)`` looks the key up in the recipient's locale, falls back to
Uzbek, and formats with ``str.format``. Every user-supplied value must be passed
through ``h()`` before it reaches a template (parse_mode is HTML).
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime

from app.bot.locales import ru as _ru
from app.bot.locales import uz as _uz
from app.services import clock

log = logging.getLogger(__name__)

LANGS: tuple[str, ...] = ("uz", "ru")
DEFAULT_LANG = "uz"

LOCALES: dict[str, dict[str, str]] = {"uz": _uz.STRINGS, "ru": _ru.STRINGS}

UZ_MONTHS = (
    "yanvar",
    "fevral",
    "mart",
    "aprel",
    "may",
    "iyun",
    "iyul",
    "avgust",
    "sentabr",
    "oktabr",
    "noyabr",
    "dekabr",
)
UZ_WEEKDAYS = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")
UZ_WD_SHORT = ("Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya")

RU_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
RU_MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
RU_WD_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def norm_lang(lang: str | None) -> str:
    return lang if lang in LOCALES else DEFAULT_LANG


def has_key(key: str) -> bool:
    return key in _uz.STRINGS


class _SafeKw(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return ""


def t(lang: str | None, key: str, **kw: object) -> str:
    table = LOCALES.get(norm_lang(lang), _uz.STRINGS)
    s = table.get(key)
    if s is None:
        s = _uz.STRINGS.get(key)
    if s is None:
        log.warning("missing i18n key %s", key)
        return key
    try:
        # a missing placeholder renders empty instead of leaking "{name}" to users
        return s.format_map(_SafeKw(kw))
    except (IndexError, ValueError):
        log.warning("bad i18n template for %s", key)
        return s


def labels(key: str) -> set[str]:
    """The key's text in every locale; used to match reply-keyboard buttons."""
    out = set()
    for table in LOCALES.values():
        if key in table:
            out.add(table[key])
    return out


def h(s: object | None) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


# ----------------------------------------------------------------- dates


def month_name(m: int, lang: str) -> str:
    return (RU_MONTHS if norm_lang(lang) == "ru" else UZ_MONTHS)[m - 1]


def weekday_name(wd: int, lang: str) -> str:
    return (RU_WEEKDAYS if norm_lang(lang) == "ru" else UZ_WEEKDAYS)[wd]


def weekday_short(wd: int, lang: str) -> str:
    return (RU_WD_SHORT if norm_lang(lang) == "ru" else UZ_WD_SHORT)[wd]


def fmt_date(d: date, lang: str) -> str:
    """'26-oktabr, dushanba' / '26 октября, понедельник'."""
    if norm_lang(lang) == "ru":
        return f"{d.day} {RU_MONTHS_GEN[d.month - 1]}, {RU_WEEKDAYS[d.weekday()]}"
    return f"{d.day}-{UZ_MONTHS[d.month - 1]}, {UZ_WEEKDAYS[d.weekday()]}"


def fmt_date_short(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def fmt_time(dt: datetime) -> str:
    """'10:30' in shop-local time."""
    return clock.to_local(dt).strftime("%H:%M")


def fmt_dt(dt: datetime, lang: str) -> str:
    """'26-oktabr, dushanba, 10:30' (shop-local)."""
    loc = clock.to_local(dt)
    return f"{fmt_date(loc.date(), lang)}, {loc.strftime('%H:%M')}"


def fmt_dt_short(dt: datetime, lang: str) -> str:
    """'Du 26.10 10:30' (shop-local) for compact buttons and lists."""
    loc = clock.to_local(dt)
    return (
        f"{weekday_short(loc.weekday(), lang)} {fmt_date_short(loc.date())} {loc.strftime('%H:%M')}"
    )


def fmt_day_btn(d: date, today: date, lang: str) -> str:
    """Bugun | Ertaga | Indinga | 'Pa 29.10'."""
    delta = (d - today).days
    if delta == 0:
        return t(lang, "common.today")
    if delta == 1:
        return t(lang, "common.tomorrow")
    if delta == 2:
        return t(lang, "common.day_after")
    return f"{weekday_short(d.weekday(), lang)} {fmt_date_short(d)}"


def fmt_price(n: int, lang: str) -> str:
    """'60 000 so'm' / '60 000 сум'."""
    s = f"{int(n):,}".replace(",", " ")
    return f"{s} {t(lang, 'common.currency')}"


def fmt_stamps(progress: int, required: int) -> str:
    progress = max(0, min(progress, required))
    return "✂️" * progress + "▫️" * (required - progress)


def fmt_minutes(n: int, lang: str) -> str:
    return t(lang, "common.minutes", n=n)
