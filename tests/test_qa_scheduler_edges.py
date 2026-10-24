"""QA regression (time_scheduler): time edges in late accept, address page, daily jobs, rate limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter

from app.bot import notifier as notifier_mod
from app.bot.callbacks import HubCb
from app.bot.scheduler import Ticker
from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, clock, schedule, users
from app.services.errors import ServiceError
from tests.bot_harness import Harness
from tests.factories import D, L, World, book


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start: datetime) -> None:
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


@pytest.fixture
def H(world: World) -> Harness:  # noqa: N802
    return Harness(world.sm)


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def test_late_accept_refused_inside_time_off_added_after_expiry(world: World) -> None:
    """EXPIRED -> late accept only re-checks booking cells, not time off / shop holiday."""
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(0), "15:00"), now=L(D(0), "11:00"))
    async with write_tx(world.sm) as s:
        expired = await bookings.expire_due_pending(s, now=L(D(0), "12:01"))
    assert [b.id for b in expired] == [r.booking.id]
    # admin closes the shop for the afternoon (nothing active overlaps, so nothing to cancel)
    async with write_tx(world.sm) as s:
        await schedule.add_time_off(
            s,
            actor_user_id=world.admin_id,
            barber_id=None,
            start_at=L(D(0), "14:00"),
            end_at=L(D(0), "20:00"),
            reason="holiday",
            cancel_conflicts=True,
            now=L(D(0), "12:02"),
        )
    # the barber taps the stale "accept" button within the 10-minute grace
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await bookings.confirm_booking(
                s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "12:05")
            )
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status != BookingStatus.CONFIRMED


async def test_address_page_says_closed_on_shop_wide_day_off(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    day_s, day_e = clock.day_bounds_utc(D(0))
    async with write_tx(world.sm) as s:
        await schedule.add_time_off(
            s,
            actor_user_id=world.admin_id,
            barber_id=None,
            start_at=day_s,
            end_at=day_e,
            reason="Bayram",
            cancel_conflicts=True,
            now=L(D(0), "08:00"),
        )
    Clock(monkeypatch, L(D(0), "11:00"))
    await H.tap(tg, HubCb(a="info").pack(), message_id=7)
    text = H.texts_to(tg)[-1]
    assert "Bugun:" in text
    assert "(ochiq)" not in text, f"shop shown as open on a shop-wide day off:\n{text}"


async def test_return_reminder_not_sent_for_deactivated_barber(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    await world.set("reviews_enabled", "false")
    start = L(D(0), "11:00")
    r = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "11:30")
        )
        await barbers.deactivate_barber(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            reason="left",
            now=L(D(1), "10:00"),
        )
    Clock(monkeypatch, start + timedelta(weeks=4, minutes=5))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    texts = H.texts_to(tg)
    assert not any("Ali sizni kutmoqda" in x for x in texts), (
        "client told a barber who left the shop is waiting, with a rebook button to him"
    )


async def test_tick_not_stalled_by_long_retry_after(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    """D-7 / T-06: one RetryAfter(300) makes the whole tick sleep 5 minutes."""
    c = await world.new_client()
    start = L(D(2), "10:00")
    await book(world, c, start)
    Clock(monkeypatch, start - timedelta(hours=24))

    orig = H.session.make_request
    raised = {"n": 0}

    async def flaky(bot: Any, method: Any, timeout: int | None = None) -> Any:
        if type(method).__name__ == "SendMessage" and raised["n"] == 0:
            raised["n"] += 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=300)
        return await orig(bot, method, timeout)

    monkeypatch.setattr(H.session, "make_request", flaky)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(sec: float, *a: Any, **k: Any) -> None:
        slept.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(notifier_mod.asyncio, "sleep", fake_sleep)
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert raised["n"] == 1
    assert sum(slept) < 60, f"tick blocked for {sum(slept):.0f}s by a single RetryAfter"


async def test_morning_summary_tells_barber_with_empty_day(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    """notify.morning_empty exists but is unreachable: idle barbers get no summary at all."""
    Clock(monkeypatch, L(D(1), "08:00"))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert H.sent_to(2000), "barber with no bookings today got no morning summary"


async def test_big_backup_alerts_admins(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,  # noqa: N803
) -> None:
    """D-6 / T-04: a backup >= 45 MB is neither sent nor reported to anyone."""
    import app.db.session as session_mod
    import app.ops.backup as backup_mod

    big = tmp_path / "shop-backup.sqlite3"
    with open(big, "wb") as f:
        f.truncate(46 * 1024 * 1024)  # sparse 46 MB file
    monkeypatch.setattr(
        session_mod, "resolve_database_url", lambda url: f"sqlite+aiosqlite:///{tmp_path}/x.db"
    )
    calls: list[str] = []
    monkeypatch.setattr(
        backup_mod, "backup_sqlite", lambda db, out: (calls.append("backup_ran"), big)[1]
    )

    async def doc(path: Any, caption: str) -> None:
        calls.append("doc")

    async def alert(key: str, **kw: object) -> None:
        calls.append(key)

    monkeypatch.setattr(H.notifier, "send_document_to_env_admins", doc)
    monkeypatch.setattr(H.notifier, "alert_admins", alert)
    await Ticker(H.bot, world.sm, H.notifier)._backup()
    assert calls[0] == "backup_ran"
    assert calls[1:], "a 46 MB backup was silently not delivered and nobody was told"
