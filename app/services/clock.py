"""Pure time helpers (ARCHITECTURE.md §4.3).

Every datetime crossing a service boundary is aware UTC. A ``date`` is a local
calendar day in SHOP_TZ. The zone is resolved lazily, never at import time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import get_config

SLOT_UNIT_MIN: int = 15
_UNIT = timedelta(minutes=SLOT_UNIT_MIN)


@lru_cache
def get_tz() -> ZoneInfo:
    return ZoneInfo(get_config().shop_tz)


def assert_tz_on_grid() -> None:
    """RuntimeError if the shop zone's UTC offset is not a whole multiple of 15 minutes."""
    tz = get_tz()
    now = datetime.now(timezone.utc)
    # check now and the next year, so DST offsets are covered too
    for days in range(0, 366, 30):
        off = (now + timedelta(days=days)).astimezone(tz).utcoffset() or timedelta(0)
        if int(off.total_seconds()) % (SLOT_UNIT_MIN * 60) != 0:
            raise RuntimeError(
                f"SHOP_TZ {tz.key!r} has UTC offset {off}, not a multiple of {SLOT_UNIT_MIN} min"
            )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC; ValueError if naive."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; pass aware UTC")
    return dt.astimezone(timezone.utc)


def _now(now: datetime | None) -> datetime:
    return now_utc() if now is None else ensure_aware(now)


def to_local(dt: datetime) -> datetime:
    return ensure_aware(dt).astimezone(get_tz())


def local_dt(day: date, t: time) -> datetime:
    """Local wall time on ``day`` -> aware UTC."""
    return datetime.combine(day, t.replace(tzinfo=None), tzinfo=get_tz()).astimezone(timezone.utc)


def local_today(now: datetime | None = None) -> date:
    return to_local(_now(now)).date()


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[local midnight of ``day``, local midnight of the next day) in UTC."""
    return local_dt(day, time(0)), local_dt(day + timedelta(days=1), time(0))


def is_on_grid(dt: datetime) -> bool:
    dt = ensure_aware(dt)
    return dt.minute % SLOT_UNIT_MIN == 0 and dt.second == 0 and dt.microsecond == 0


def floor_to_grid(dt: datetime) -> datetime:
    dt = ensure_aware(dt)
    return dt.replace(minute=dt.minute - dt.minute % SLOT_UNIT_MIN, second=0, microsecond=0)


def ceil_to_grid(dt: datetime) -> datetime:
    dt = ensure_aware(dt)
    if is_on_grid(dt):
        return dt
    return floor_to_grid(dt) + _UNIT


def clamp_to_daytime(dt: datetime, *, start: time = time(10), end: time = time(21)) -> datetime:
    """Move ``dt`` forward into local [start, end); used for ret/rev sends."""
    local = to_local(dt)
    lt = local.time().replace(tzinfo=None)
    if lt < start:
        return local_dt(local.date(), start)
    if lt >= end:
        return local_dt(local.date() + timedelta(days=1), start)
    return ensure_aware(dt)
