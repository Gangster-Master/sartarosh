"""QA regression (time_scheduler): outbox rows outlive the settings / user state that created them."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.bot.scheduler import Ticker
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, users
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


async def test_reminder_24h_not_sent_after_admin_disables_it(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    start = L(D(2), "10:00")
    await book(world, c, start)
    # admin turns the 24 h reminder off after the booking was made
    await world.set("reminder_24h_enabled", "false")
    Clock(monkeypatch, start - timedelta(hours=24))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert H.sent_to(tg) == [], "24h reminder delivered although the setting is off"


async def test_review_request_not_sent_after_reviews_disabled(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    start = L(D(0), "11:00")
    r = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "11:30")
        )
    await world.set("reviews_enabled", "false")
    Clock(monkeypatch, L(D(0), "14:00"))  # end 11:30 + 2 h
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert H.sent_to(tg) == [], "review request sent although reviews are disabled"


async def test_return_reminder_not_sent_after_admin_sets_weeks_zero(
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
    await world.set("return_reminder_weeks", "0")  # "0 = off"
    Clock(monkeypatch, start + timedelta(weeks=4, minutes=5))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert H.sent_to(tg) == [], "return reminder sent although return reminders are off"


async def test_blocked_client_gets_no_return_reminder(
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
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=c, blocked=True)
    Clock(monkeypatch, start + timedelta(weeks=4, minutes=5))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    # the reminder invites the client to rebook, which a blocked user cannot do
    assert H.sent_to(tg) == [], "blocked client was invited to rebook"
