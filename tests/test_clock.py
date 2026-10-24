from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo

import pytest

from app.bot.i18n import fmt_date, fmt_day_btn
from app.services import clock
from tests.factories import TZ


def test_day_bounds_utc() -> None:
    s, e = clock.day_bounds_utc(date(2026, 10, 26))
    assert s == datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)
    assert e - s == timedelta(days=1)


def test_local_dt_and_today() -> None:
    dt = clock.local_dt(date(2026, 10, 26), time(10, 30))
    assert dt == datetime(2026, 10, 26, 5, 30, tzinfo=timezone.utc)
    # 23:30 local Sunday is still Sunday locally although UTC says 18:30 Sunday
    late = datetime(2026, 10, 25, 23, 30, tzinfo=TZ)
    assert clock.local_today(late) == date(2026, 10, 25)
    # 00:30 local Monday is Sunday in UTC
    early = datetime(2026, 10, 26, 0, 30, tzinfo=TZ)
    assert clock.local_today(early) == date(2026, 10, 26)


def test_grid() -> None:
    dt = datetime(2026, 10, 26, 5, 7, 13, tzinfo=timezone.utc)
    assert clock.floor_to_grid(dt) == datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)
    assert clock.ceil_to_grid(dt) == datetime(2026, 10, 26, 5, 15, tzinfo=timezone.utc)
    on = datetime(2026, 10, 26, 5, 45, tzinfo=timezone.utc)
    assert clock.ceil_to_grid(on) == on and clock.is_on_grid(on)
    assert not clock.is_on_grid(dt)
    with pytest.raises(ValueError):
        clock.floor_to_grid(datetime(2026, 1, 1, 10, 0))
    with pytest.raises(ValueError):
        clock.ensure_aware(datetime(2026, 1, 1))


def test_assert_tz_on_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    clock.assert_tz_on_grid()  # Tashkent: +05:00
    from zoneinfo import ZoneInfo

    # NB: ARCH §10 names Asia/Kathmandu, but +05:45 IS a multiple of 15 minutes,
    # so a 15-minute grid works there; it must pass.
    monkeypatch.setattr(clock, "get_tz", lambda: ZoneInfo("Asia/Kathmandu"))
    clock.assert_tz_on_grid()

    class OddTz(tzinfo):
        key = "Test/Plus0020"

        def utcoffset(self, dt):  # type: ignore[no-untyped-def]
            return timedelta(minutes=20)

        def dst(self, dt):  # type: ignore[no-untyped-def]
            return timedelta(0)

    monkeypatch.setattr(clock, "get_tz", lambda: OddTz())
    with pytest.raises(RuntimeError):
        clock.assert_tz_on_grid()


def test_clamp_to_daytime() -> None:
    d = date(2026, 10, 26)
    early = clock.local_dt(d, time(7, 0))
    assert clock.clamp_to_daytime(early) == clock.local_dt(d, time(10))
    mid = clock.local_dt(d, time(15, 0))
    assert clock.clamp_to_daytime(mid) == mid
    late = clock.local_dt(d, time(21, 0))
    assert clock.clamp_to_daytime(late) == clock.local_dt(d + timedelta(days=1), time(10))


def test_weekday_labels_from_calendar() -> None:
    # 2026-10-26 is a Monday; 29.10 is Thursday (Payshanba)
    assert fmt_date(date(2026, 10, 26), "uz") == "26-oktabr, dushanba"
    today = date(2026, 10, 26)
    assert fmt_day_btn(today, today, "uz") == "Bugun"
    assert fmt_day_btn(today + timedelta(days=1), today, "uz") == "Ertaga"
    assert fmt_day_btn(today + timedelta(days=2), today, "uz") == "Indinga"
    assert fmt_day_btn(date(2026, 10, 29), today, "uz") == "Pa 29.10"
