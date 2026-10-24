from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, users
from app.services.errors import TooLateError
from tests.factories import D, L, World, book


async def _no_show(w: World, c: int, day: int) -> None:
    start = L(D(day), "10:00")
    r = await book(w, c, start, now=start - timedelta(hours=1))
    async with write_tx(w.sm) as s:
        await bookings.mark_no_show(
            s,
            booking_id=r.booking.id,
            actor_user_id=w.ali_user_id,
            now=start + timedelta(minutes=20),
        )


async def test_soft_block_after_three(world: World) -> None:
    c = await world.new_client()
    for d in (0, 1, 2):
        await _no_show(world, c, d)
    async with read_tx(world.sm) as s:
        assert await users.is_soft_blocked(s, c, now=L(D(3), "08:00"))
    r = await book(world, c, L(D(3), "11:00"), now=L(D(3), "08:00"))
    assert r.booking.status == BookingStatus.PENDING and r.client_soft_blocked
    assert {t.role for t in r.decision_targets} == {"barber", "admin"}
    # admin reset lifts it
    async with write_tx(world.sm) as s:
        await users.reset_no_shows(s, actor_user_id=world.admin_id, user_id=c, now=L(D(3), "08:30"))
    async with read_tx(world.sm) as s:
        assert not await users.is_soft_blocked(s, c, now=L(D(3), "09:00"))
    r = await book(world, c, L(D(4), "11:00"), now=L(D(3), "09:00"))
    assert r.booking.status == BookingStatus.CONFIRMED


async def test_soft_block_window(world: World) -> None:
    c = await world.new_client()
    for d in (0, 1, 2):
        await _no_show(world, c, d)
    async with read_tx(world.sm) as s:
        assert not await users.is_soft_blocked(s, c, now=L(D(95), "08:00"))


async def test_late_cancels_count_when_configured(world: World) -> None:
    await world.set("late_cancels_per_no_show", "2")
    await world.set("soft_block_no_shows", "1")
    c = await world.new_client()
    for d in (1, 2):
        start = L(D(d), "15:00")
        r = await book(world, c, start, now=start - timedelta(hours=3))
        async with write_tx(world.sm) as s:
            res = await bookings.cancel_by_client(
                s, booking_id=r.booking.id, client_user_id=c, now=start - timedelta(hours=1)
            )
        assert res.late
    async with read_tx(world.sm) as s:
        assert await users.is_soft_blocked(s, c, now=L(D(3), "08:00"))


async def _auto_completed(w: World) -> tuple[int, int]:
    c = await w.new_client()
    start = L(D(0), "10:00")
    r = await book(w, c, start)
    async with write_tx(w.sm) as s:
        done = await bookings.auto_complete_due(s, now=start + timedelta(minutes=30, hours=11))
        assert done == []
    async with write_tx(w.sm) as s:
        done = await bookings.auto_complete_due(s, now=start + timedelta(minutes=30, hours=12))
    assert [d.booking.id for d in done] == [r.booking.id]
    assert done[0].booking.auto_completed and done[0].earned == 1 and done[0].welcome_credited == 1
    return r.booking.id, c


async def test_auto_completed_flip_reverses_bonus(world: World) -> None:
    bid, c = await _auto_completed(world)
    async with read_tx(world.sm) as s:
        assert (await bonus.get_bonus_status(s, c)).balance == 2
        completed_at = (await bookings.get_booking(s, bid)).completed_at
    async with write_tx(world.sm) as s:
        r = await bookings.mark_no_show(
            s,
            booking_id=bid,
            actor_user_id=world.ali_user_id,
            now=completed_at + timedelta(hours=23),
        )
    assert r.booking.status == BookingStatus.NO_SHOW and r.reversed == 2
    async with read_tx(world.sm) as s:
        st = await bonus.get_bonus_status(s, c)
        u = await users.get_user(s, c)
        assert st.balance == 0 and await bonus.recompute_balance(s, c) == 0
    assert u.no_show_count == 1


async def test_auto_completed_flip_window_passed(world: World) -> None:
    bid, _ = await _auto_completed(world)
    async with read_tx(world.sm) as s:
        completed_at = (await bookings.get_booking(s, bid)).completed_at
    async with write_tx(world.sm) as s:
        with pytest.raises(TooLateError):
            await bookings.mark_no_show(
                s,
                booking_id=bid,
                actor_user_id=world.ali_user_id,
                now=completed_at + timedelta(hours=24, minutes=1),
            )


async def test_list_unresolved(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "10:00"))
    async with read_tx(world.sm) as s:
        assert await bookings.list_unresolved(s, now=L(D(0), "10:29")) == []
        out = await bookings.list_unresolved(s, now=L(D(0), "10:31"))
    assert [b.id for b in out] == [r.booking.id]
