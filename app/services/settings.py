"""Shop settings, job markers and the setup token (ARCHITECTURE.md §4.4)."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, fields
from datetime import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.enums import ACTIVE_STATUSES, LoyaltyMode
from app.db.models import Booking, Setting, User
from app.services import clock
from app.services._common import require_admin
from app.services.audit import write_audit
from app.services.dto import ShopSettings
from app.services.errors import InputError

DEFAULTS: dict[str, object] = {
    # booking
    "booking_horizon_days": 7, "min_lead_min": 30, "free_cancel_hours": 2,
    "max_active_bookings": 3, "max_requests_per_day": 5,
    # pending / decision
    "pending_timeout_min": 60, "pending_cutoff_before_start_min": 10, "min_decision_min": 15,
    "pending_reping_min": 15, "proposal_reply_min": 30, "late_accept_grace_min": 10,
    "new_client_requires_approval": True,
    # reminders / retention
    "reminder_24h_enabled": True, "reminder_2h_enabled": True,
    "return_reminder_weeks": 4, "reviews_enabled": True, "review_delay_hours": 2,
    # no-show policy
    "late_cancels_per_no_show": 0, "soft_block_no_shows": 3, "soft_block_window_days": 90,
    "auto_complete_after_hours": 12, "noshow_flip_window_hours": 24,
    # loyalty
    "loyalty_enabled": True, "loyalty_mode": "stamps",
    "bonus_required_stamps": 5, "bonus_reward_type": "free", "bonus_reward_value": 100,
    "cashback_percent": 5, "cashback_max_redeem_percent": 50,
    "welcome_bonus": 1, "referral_enabled": True, "referral_bonus": 1,
    # staff
    "barbers_can_block_time": True, "allow_foreign_phone": True,
    # jobs (local time "HH:MM")
    "daily_summary_time": "08:00", "nightly_check_time": "23:00", "backup_time": "03:30",
    # shop info
    "shop_name": "", "address": "", "latitude": None, "longitude": None, "contact_phone": "",
    "map_yandex_url": "", "map_google_url": "", "map_2gis_url": "",
}  # fmt: skip

SETUP_TOKEN_KEY = "_setup_token"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    type: str  # "int" | "bool" | "str" | "time" | "float" | "choice"
    group: str  # booking | pending | reminders | noshow | loyalty | staff | jobs | shop
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    max_len: int = 200
    nullable: bool = False


def _i(group: str, lo: int, hi: int) -> SettingSpec:
    return SettingSpec("int", group, lo, hi)


def _b(group: str) -> SettingSpec:
    return SettingSpec("bool", group)


SETTING_SPECS: dict[str, SettingSpec] = {
    "booking_horizon_days": _i("booking", 1, 60),
    "min_lead_min": _i("booking", 0, 1440),
    "free_cancel_hours": _i("booking", 0, 72),
    "max_active_bookings": _i("booking", 1, 20),
    "max_requests_per_day": _i("booking", 1, 50),
    "pending_timeout_min": _i("pending", 5, 1440),
    "pending_cutoff_before_start_min": _i("pending", 0, 240),
    "min_decision_min": _i("pending", 5, 240),
    "pending_reping_min": _i("pending", 5, 240),
    "proposal_reply_min": _i("pending", 5, 1440),
    "late_accept_grace_min": _i("pending", 0, 120),
    "new_client_requires_approval": _b("pending"),
    "reminder_24h_enabled": _b("reminders"),
    "reminder_2h_enabled": _b("reminders"),
    "return_reminder_weeks": _i("reminders", 0, 26),
    "reviews_enabled": _b("reminders"),
    "review_delay_hours": _i("reminders", 0, 72),
    "late_cancels_per_no_show": _i("noshow", 0, 10),
    "soft_block_no_shows": _i("noshow", 0, 20),
    "soft_block_window_days": _i("noshow", 1, 365),
    "auto_complete_after_hours": _i("noshow", 1, 72),
    "noshow_flip_window_hours": _i("noshow", 0, 168),
    "loyalty_enabled": _b("loyalty"),
    "loyalty_mode": SettingSpec("choice", "loyalty", choices=("stamps", "cashback")),
    "bonus_required_stamps": _i("loyalty", 1, 50),
    "bonus_reward_type": SettingSpec("choice", "loyalty", choices=("free", "percent")),
    "bonus_reward_value": _i("loyalty", 1, 100),
    "cashback_percent": _i("loyalty", 0, 100),
    "cashback_max_redeem_percent": _i("loyalty", 0, 100),
    "welcome_bonus": _i("loyalty", 0, 1_000_000),
    "referral_enabled": _b("loyalty"),
    "referral_bonus": _i("loyalty", 0, 1_000_000),
    "barbers_can_block_time": _b("staff"),
    "allow_foreign_phone": _b("staff"),
    "daily_summary_time": SettingSpec("time", "jobs"),
    "nightly_check_time": SettingSpec("time", "jobs"),
    "backup_time": SettingSpec("time", "jobs"),
    "shop_name": SettingSpec("str", "shop", max_len=64),
    "address": SettingSpec("str", "shop", max_len=300),
    "latitude": SettingSpec("float", "shop", -90, 90, nullable=True),
    "longitude": SettingSpec("float", "shop", -180, 180, nullable=True),
    "contact_phone": SettingSpec("str", "shop", max_len=32),
    "map_yandex_url": SettingSpec("str", "shop", max_len=300),
    "map_google_url": SettingSpec("str", "shop", max_len=300),
    "map_2gis_url": SettingSpec("str", "shop", max_len=300),
}

_TRUE = {"1", "true", "yes", "on", "ha", "xa", "да", "y", "+"}
_FALSE = {"0", "false", "no", "off", "yo'q", "yoq", "yo`q", "yoʻq", "нет", "n", "-"}
_EMPTY = {"", "-", "—", "none", "null", "yo'q"}


def _parse_time(raw: str) -> time:
    s = raw.strip().replace(".", ":")
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(raw)
    h, m = int(parts[0]), int(parts[1])
    return time(h, m)


def _coerce(key: str, value: Any) -> Any:
    """Coerce a stored JSON value to the ShopSettings field type (lenient)."""
    spec = SETTING_SPECS.get(key)
    if spec is None:
        return value
    try:
        if value is None:
            return None if spec.nullable else _coerce(key, DEFAULTS[key])
        if spec.type == "int":
            return int(value)
        if spec.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in _TRUE
            return bool(value)
        if spec.type == "float":
            return float(value)
        if spec.type == "time":
            return value if isinstance(value, time) else _parse_time(str(value))
        return str(value)
    except (TypeError, ValueError):
        return _coerce(key, DEFAULTS[key]) if value != DEFAULTS[key] else None


def parse_value(key: str, raw_value: str) -> Any:
    """Parse admin input per spec -> JSON-storable value. InputError(field=key)."""
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise InputError("unknown setting", field=key)
    raw = (raw_value or "").strip()
    try:
        if spec.type == "int":
            v: Any = int(raw.replace(" ", ""))
        elif spec.type == "float":
            if spec.nullable and raw.lower() in _EMPTY:
                return None
            v = float(raw.replace(",", "."))
        elif spec.type == "bool":
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(raw)
        elif spec.type == "time":
            t = _parse_time(raw)
            return f"{t.hour:02d}:{t.minute:02d}"
        elif spec.type == "choice":
            if raw.lower() not in spec.choices:
                raise ValueError(raw)
            return raw.lower()
        else:
            if raw in ("-", "—"):
                raw = ""
            if len(raw) > spec.max_len:
                raise InputError("too long", field=key, limit=spec.max_len)
            return raw
    except InputError:
        raise
    except (TypeError, ValueError):
        raise InputError("invalid value", field=key) from None
    if spec.min is not None and v < spec.min:
        raise InputError("too small", field=key, min=spec.min, max=spec.max)
    if spec.max is not None and v > spec.max:
        raise InputError("too large", field=key, min=spec.min, max=spec.max)
    return v


async def _raw_settings(session: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (await session.execute(select(Setting.key, Setting.value))).all():
        if key.startswith("_"):
            continue
        try:
            out[key] = json.loads(value) if value is not None else None
        except ValueError:
            continue
    return out


def _build(values: dict[str, Any]) -> ShopSettings:
    kw: dict[str, Any] = {}
    for f in fields(ShopSettings):
        raw = values.get(f.name, DEFAULTS.get(f.name))
        kw[f.name] = _coerce(f.name, raw)
    return ShopSettings(**kw)


async def get_shop_settings(session: AsyncSession) -> ShopSettings:
    return _build(await _raw_settings(session))


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    data = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=key, value=data, updated_at=clock.now_utc()))
    else:
        row.value = data
        row.updated_at = clock.now_utc()
    await session.flush()


async def update_setting(
    session: AsyncSession, *, actor_user_id: int, key: str, raw_value: str
) -> ShopSettings:
    await require_admin(session, actor_user_id)
    if key.startswith("_") or key not in SETTING_SPECS:
        raise InputError("unknown setting", field=key)
    value = parse_value(key, raw_value)
    if key.startswith("map_") and key.endswith("_url") and value:
        from app.services.validation import is_web_url

        if not is_web_url(str(value)):
            raise InputError("invalid url", field=key)
    current = await _raw_settings(session)
    merged = {**DEFAULTS, **current, key: value}
    s_new = _build(merged)

    # cross-field rule [B2]
    if key in ("min_lead_min", "pending_cutoff_before_start_min", "min_decision_min"):
        need = s_new.pending_cutoff_before_start_min + s_new.min_decision_min
        if s_new.min_lead_min < need:
            raise InputError(
                "min_lead_min too small", field=key, reason="lead_lt_cutoff_decision", min=need
            )

    if key == "loyalty_mode":
        s_old = _build({**DEFAULTS, **current})
        if s_old.loyalty_mode != s_new.loyalty_mode:
            has_balance = (
                await session.execute(
                    select(func.count()).select_from(User).where(User.bonus_balance != 0)
                )
            ).scalar_one()
            has_reserved = (
                await session.execute(
                    select(func.count())
                    .select_from(Booking)
                    .where(Booking.status.in_(list(ACTIVE_STATUSES)), Booking.bonus_cost > 0)
                )
            ).scalar_one()
            if has_balance or has_reserved:
                raise InputError("balances exist", field="loyalty_mode", reason="balances_exist")
            LoyaltyMode(s_new.loyalty_mode)  # validate

    await _put(session, key, value)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="setting.update",
        entity="setting",
        entity_id=None,
        payload={"key": key, "value": value},
    )
    return s_new


async def set_location(
    session: AsyncSession, *, actor_user_id: int, latitude: float, longitude: float
) -> ShopSettings:
    await require_admin(session, actor_user_id)
    if not -90 <= float(latitude) <= 90:
        raise InputError("bad latitude", field="latitude")
    if not -180 <= float(longitude) <= 180:
        raise InputError("bad longitude", field="longitude")
    await _put(session, "latitude", float(latitude))
    await _put(session, "longitude", float(longitude))
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="setting.update",
        entity="setting",
        entity_id=None,
        payload={"key": "location", "latitude": latitude, "longitude": longitude},
    )
    return await get_shop_settings(session)


async def get_marker(session: AsyncSession, key: str) -> str | None:
    """Internal "_job.*" markers."""
    row = await session.get(Setting, key)
    if row is None or row.value is None:
        return None
    try:
        v = json.loads(row.value)
    except ValueError:
        return row.value
    return None if v is None else str(v)


async def set_marker(session: AsyncSession, key: str, value: str) -> None:
    if not key.startswith("_"):
        raise InputError("markers must start with '_'", field="key")
    await _put(session, key, value)


async def ensure_setup_token(session: AsyncSession) -> str | None:
    """Create (or keep) "_setup_token" while no admin exists; otherwise delete it [G1]."""
    has_env = bool(get_config().admin_ids)
    promoted = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.deleted_at.is_(None))
        )
    ).scalar_one()
    if has_env or promoted:
        await session.execute(delete(Setting).where(Setting.key == SETUP_TOKEN_KEY))
        return None
    existing = await get_marker(session, SETUP_TOKEN_KEY)
    if existing:
        return existing
    token = secrets.token_urlsafe(16)
    await _put(session, SETUP_TOKEN_KEY, token)
    return token
