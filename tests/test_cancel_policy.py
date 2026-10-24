from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.uow import read_tx, write_tx
from app.services import bookings, users
from app.services.errors import InvalidTransitionError, TooLateError
from tests.factories import D, L, World, book

START = L(D(1), "15:00")


async def _late_count(w: World, c: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, c)).late_cancel_count


async def _cancel(w: World, bid: int, c: int, now):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await bookings.cancel_by_client(s, booking_id=bid, client_user_id=c, now=now)


async def test_free_cancel_more_than_2h(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, START)
    res = await _cancel(world, r.booking.id, c, START - timedelta(hours=2, minutes=1))
    assert not res.late and await _late_count(world, c) == 0


async def test_late_cancel_confirmed(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, START)
    res = await _cancel(world, r.booking.id, c, START - timedelta(hours=1))
    assert res.late and res.booking.late_cancel
    assert await _late_count(world, c) == 1


async def test_withdraw_pending_not_late(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, START, now=START - timedelta(hours=1, minutes=30))
    res = await _cancel(world, r.booking.id, c, START - timedelta(hours=1))
    assert not res.late and await _late_count(world, c) == 0


async def test_past_booking(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, START)
    with pytest.raises(TooLateError):
        await _cancel(world, r.booking.id, c, START)
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=START
        )
    with pytest.raises(InvalidTransitionError):
        await _cancel(world, r.booking.id, c, START)
