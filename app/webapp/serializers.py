"""DTO -> JSON dicts (docs/WEBAPP.md §4.2). Pure and sync: no DB, no network.

Every instant is ISO-8601 UTC with ``Z`` plus a ``local`` object computed in SHOP_TZ;
the frontend never converts time zones. Labels come from the bot's own i18n.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.bot.i18n import fmt_date, has_key, t, weekday_short
from app.config import CONSENT_VERSION
from app.db.enums import Awaiting, BookingStatus
from app.services import clock, validation
from app.services.dto import (
    BarberDTO,
    BonusStatus,
    BookingDTO,
    ClientStats,
    DayAvailability,
    ShopDayHours,
    ShopSettings,
    UserContext,
)
from app.services.errors import InvalidNameError

LANG = "uz"
_TAG_RE = re.compile(r"<[^>]+>")

DONE_BEFORE = timedelta(minutes=30)  # complete_booking: not earlier than start - 30 min
NO_SHOW_AFTER = timedelta(minutes=15)  # mark_no_show: not earlier than start + 15 min


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain(text: str) -> str:
    """Bot HTML text -> plain text (frontend renders with textContent only)."""
    return html.unescape(_TAG_RE.sub("", text or ""))


def hhmm(tm: time | None) -> str | None:
    return None if tm is None else f"{tm.hour:02d}:{tm.minute:02d}"


def local_json(start: datetime | None, end: datetime | None = None) -> dict[str, Any] | None:
    if start is None:
        return None
    loc = clock.to_local(start)
    return {
        "date": loc.date().isoformat(),
        "time": loc.strftime("%H:%M"),
        "end_time": clock.to_local(end).strftime("%H:%M") if end is not None else None,
        "weekday": loc.weekday(),
        "label": fmt_date(loc.date(), LANG),
    }


def _now(now: datetime | None) -> datetime:
    return clock.ensure_aware(now) if now is not None else clock.now_utc()


# ----------------------------------------------------------------- user


def name_ok(name: str | None) -> bool:
    try:
        validation.validate_name(name or "")
    except InvalidNameError:
        return False
    return True


def user_json(ctx: UserContext, bonus: BonusStatus | None = None) -> dict[str, Any]:
    u, r = ctx.user, ctx.roles
    out: dict[str, Any] = {
        "id": u.id,
        "first_name": u.first_name,
        "display_name": u.display_name,
        "phone": u.phone,
        "phone_verified": bool(u.phone and u.phone_verified),
        "name_ok": name_ok(u.display_name),
        "lang": u.lang,
        "roles": {
            "client": r.is_client,
            "barber": bool(r.is_barber and r.barber_id),
            "admin": r.is_admin,
            "barber_id": r.barber_id,
        },
        "consent_required": (u.consent_version or 0) < CONSENT_VERSION,
        "blocked": u.is_blocked,
    }
    if bonus is not None:
        out["bonus"] = bonus_json(bonus, recent=False)
    return out


# ----------------------------------------------------------------- bookings


def status_label(b: BookingDTO) -> str:
    if b.status == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT:
        return t(LANG, "status.PENDING_CLIENT")
    return t(LANG, f"status.{b.status.value}")


def reason_label(reason: str | None) -> str | None:
    if not reason:
        return None
    key = f"reason.{reason}"
    return t(LANG, key) if has_key(key) else reason


def stats_json(stats: ClientStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {
        "visits": stats.total_completed,
        "no_shows": stats.no_show_count,
        "late_cancels": stats.late_cancel_count,
        "is_new": stats.is_new,
    }


def client_actions(b: BookingDTO, shop: ShopSettings, now: datetime) -> dict[str, Any]:
    """Same predicates as client_my; the service still decides."""
    active = b.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED) and b.start_at > now
    awaiting_barber = b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER
    return {
        "cancel": active and not awaiting_barber,
        "withdraw": active and awaiting_barber,
        "late_cancel_warning": b.status == BookingStatus.CONFIRMED
        and b.start_at > now
        and b.start_at - now < timedelta(hours=shop.free_cancel_hours),
        "answer_proposal": b.status == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT,
        "accept": False,
        "reject": False,
        "done": False,
        "no_show": False,
        "done_from": None,
        "no_show_from": None,
    }


def barber_actions(b: BookingDTO, shop: ShopSettings, now: datetime) -> dict[str, Any]:
    """Mirrors confirm_booking / reject_booking / complete_booking / mark_no_show [J-D5]."""
    pending_barber = b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER
    late_accept = (
        b.status == BookingStatus.EXPIRED
        and b.awaiting == Awaiting.BARBER
        and b.expire_at is not None
        and now <= b.expire_at + timedelta(minutes=shop.late_accept_grace_min)
        and b.start_at > now
    )
    confirmed = b.status == BookingStatus.CONFIRMED
    return {
        "cancel": False,
        "withdraw": False,
        "late_cancel_warning": False,
        "answer_proposal": False,
        "accept": (pending_barber and b.start_at > now) or late_accept,
        "reject": pending_barber,
        "done": confirmed,
        "no_show": confirmed,
        "done_from": iso(b.start_at - DONE_BEFORE) if confirmed else None,
        "no_show_from": iso(b.start_at + NO_SHOW_AFTER) if confirmed else None,
    }


def booking_json(
    b: BookingDTO,
    *,
    viewer: str,
    shop: ShopSettings,
    now: datetime | None = None,
    stats: ClientStats | None = None,
) -> dict[str, Any]:
    now = _now(now)
    actions = barber_actions(b, shop, now) if viewer == "barber" else client_actions(b, shop, now)
    out: dict[str, Any] = {
        "id": b.id,
        "status": b.status.value,
        "awaiting": b.awaiting.value if b.awaiting else None,
        "source": b.source.value,
        "status_label": status_label(b),
        "barber": {"id": b.barber_id, "name": b.barber_name},
        "service": {"id": b.service_id, "name": b.service_name} if b.service_name else None,
        "start_at": iso(b.start_at),
        "end_at": iso(b.end_at),
        "local": local_json(b.start_at, b.end_at),
        "duration_min": b.duration_min,
        "price_uzs": b.price_snapshot,
        "final_price_uzs": b.final_price_uzs,
        "bonus_redeemed": b.bonus_redeemed,
        "bonus_cost": b.bonus_cost,
        "comment": b.comment,
        "expire_at": iso(b.expire_at),
        "original_start_at": iso(b.original_start_at),
        "original_local": local_json(b.original_start_at) if b.original_start_at else None,
        "proposal_count": b.proposal_count,
        "late_cancel": b.late_cancel,
        "cancel_reason": b.cancel_reason,
        "cancel_reason_label": reason_label(b.cancel_reason),
        "created_at": iso(b.created_at),
        "actions": actions,
        "done_from": actions["done_from"],
        "no_show_from": actions["no_show_from"],
    }
    if viewer == "barber":
        out["client"] = {
            "name": b.client_name,
            "phone": b.client_phone,
            "phone_masked": bool(b.client_phone and "*" in b.client_phone),
            "stats": stats_json(stats),
        }
        out["auto_completed"] = b.auto_completed
    return out


# ----------------------------------------------------------------- days / slots


def day_info(d: date, today: date | None = None) -> dict[str, Any]:
    today = today or clock.local_today()
    return {
        "day": d.isoformat(),
        "label": fmt_date(d, LANG),
        "short": f"{weekday_short(d.weekday(), LANG)} {d.day}",
        "weekday": d.weekday(),
        "is_today": d == today,
        "is_tomorrow": d == today + timedelta(days=1),
    }


def day_json(a: DayAvailability, today: date | None = None) -> dict[str, Any]:
    out = day_info(a.day, today)
    out["is_working"] = a.is_working
    out["has_free_slots"] = a.has_free_slots
    return out


def slot_group(dt: datetime) -> str:
    h = clock.to_local(dt).hour
    return "morning" if h < 12 else ("day" if h < 17 else "evening")


def slot_json(dt: datetime) -> dict[str, Any]:
    return {
        "start_at": iso(dt),
        "time": clock.to_local(dt).strftime("%H:%M"),
        "group": slot_group(dt),
    }


# ----------------------------------------------------------------- bonus / shop


def bonus_json(st: BonusStatus, *, recent: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mode": st.mode.value,
        "enabled": st.enabled,
        "balance": st.balance,
        "reserved": st.reserved,
        "available": st.available,
        "required": st.required,
        "progress": st.progress,
        "can_redeem": st.can_redeem,
        "reward_type": st.reward_type,
        "reward_value": st.reward_value,
        "cashback_percent": st.cashback_percent,
        "max_redeem_percent": st.max_redeem_percent,
    }
    if recent:
        out["recent"] = [
            {
                "id": tx.id,
                "delta": tx.delta,
                "unit": tx.unit.value,
                "reason": tx.reason.value,
                "reason_label": t(LANG, f"bonus.reason.{tx.reason.value}"),
                "comment": tx.comment,
                "created_at": iso(tx.created_at),
                "local": local_json(tx.created_at),
            }
            for tx in st.recent
        ]
    return out


def map_url(shop: ShopSettings) -> str | None:
    if shop.map_google_url:
        return shop.map_google_url
    if shop.map_yandex_url:
        return shop.map_yandex_url
    if shop.latitude is not None and shop.longitude is not None:
        return f"https://maps.google.com/?q={shop.latitude},{shop.longitude}"
    return None


def shop_json(shop: ShopSettings, week: list[ShopDayHours]) -> dict[str, Any]:
    return {
        "name": shop.shop_name or None,
        "address": shop.address or None,
        "phone": shop.contact_phone or None,
        "latitude": shop.latitude,
        "longitude": shop.longitude,
        "map_yandex_url": shop.map_yandex_url or None,
        "map_google_url": shop.map_google_url or None,
        "map_url": map_url(shop),
        "hours": [
            {"weekday": d.weekday, "open": hhmm(d.open_time), "close": hhmm(d.close_time)}
            for d in week
        ],
        "free_cancel_hours": shop.free_cancel_hours,
        "booking_horizon_days": shop.booking_horizon_days,
    }


def photo_url(b: BarberDTO) -> str | None:
    if not b.photo_file_id:
        return None
    v = hashlib.sha1(b.photo_file_id.encode()).hexdigest()[:8]  # noqa: S324 - cache buster
    return f"/api/barbers/{b.id}/photo?v={v}"
