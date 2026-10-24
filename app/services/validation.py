"""Pure input validation (ARCHITECTURE.md §4.5)."""

from __future__ import annotations

import re
import unicodedata

from app.services.errors import InputError, InvalidNameError, InvalidPhoneError

PHONE_RE = re.compile(r"^\+998\d{9}$")
_STRIP_RE = re.compile(r"[\s\-()]")
_NAME_EXTRA = set(" '`-ʻʼ‘’")  # ' ` - ʻ ʼ ‘ ’
NAME_MIN, NAME_MAX = 2, 50
COMMENT_MAX = 300


def normalize_phone(raw: str) -> str:
    """'+998XXXXXXXXX' | '998XXXXXXXXX' | 'XXXXXXXXX' (spaces, '-', '(', ')' ignored)."""
    s = _STRIP_RE.sub("", raw or "")
    if s.startswith("+"):
        body = s[1:]
        if body.isdigit() and body.startswith("998") and len(body) == 12:
            return "+" + body
    elif s.isdigit():
        if s.startswith("998") and len(s) == 12:
            return "+" + s
        if len(s) == 9:
            return "+998" + s
    raise InvalidPhoneError("invalid phone", field="phone")


def normalize_contact_phone(raw: str) -> tuple[str, bool]:
    """Telegram ``request_contact`` value -> ('+digits', is_foreign)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 9:  # some clients send the national part only
        digits = "998" + digits
    if not 7 <= len(digits) <= 15:
        raise InvalidPhoneError("invalid phone", field="phone")
    phone = "+" + digits
    return phone, PHONE_RE.match(phone) is None


def validate_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not NAME_MIN <= len(name) <= NAME_MAX:
        raise InvalidNameError("invalid name", field="name")
    if not any(ch.isalpha() for ch in name):
        raise InvalidNameError("invalid name", field="name")
    for ch in name:
        if not (ch.isalpha() or ch in _NAME_EXTRA):
            raise InvalidNameError("invalid name", field="name")
    return name


def validate_comment(raw: str) -> str | None:
    if raw is None:
        return None
    text = "".join(
        ch for ch in str(raw) if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf")
    ).strip()
    if not text:
        return None
    if len(text) > COMMENT_MAX:
        raise InputError("comment too long", field="comment", limit=COMMENT_MAX)
    return text


def mask_phone(phone: str) -> str:
    """'+998901234567' -> '+998 90 *** ** 67'."""
    if not phone:
        return ""
    if PHONE_RE.match(phone):
        return f"+998 {phone[4:6]} *** ** {phone[-2:]}"
    if len(phone) <= 6:
        return "*" * len(phone)
    return f"{phone[:4]} *** {phone[-2:]}"


def phone_hash(phone: str | None) -> str | None:
    """Stable one-way key for a normalized phone (used for deleted users)."""
    if not phone:
        return None
    import hashlib

    return hashlib.sha256(f"sartarosh:phone:{phone}".encode()).hexdigest()


def is_web_url(url: str | None) -> bool:
    """http(s) URL with a host and no whitespace (Telegram rejects other button URLs)."""
    if not url or any(ch.isspace() for ch in url):
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and "." in parts.hostname
