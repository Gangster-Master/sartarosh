from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.enums import ACTIVE_STATUSES, Awaiting, BookingStatus, CancelledBy
from app.db.models import BookingSlot
from app.db.uow import read_tx, write_tx
from app.services import bookings, slots
from app.services.errors import (
    InputError,
    InvalidTransitionError,
    PermissionDeniedError,
    SlotTakenError,
    TooEarlyError,
    TooLateError,
)
from tests.factories import NOW, D, L, World, book, count_rows

START = L(D(1), "10:00")


async def _pending(w: World) -> tuple[int, int]:
    await w.set("new_client_requires_approval", "true")
    c = await w.new_client()
    r = await book(w, c, START)
    assert r.booking.status == BookingStatus.PENDING
    return r.booking.id, c


async def _confirmed(w: World) -> tuple[int, int]:
    c = await w.new_client()
    r = await book(w, c, START)
    assert r.booking.status == BookingStatus.CONFIRMED
    return r.booking.id, c


async def _cells(w: World) -> int:
    return await count_rows(w.sm, BookingSlot)


async def _slot_free(w: World) -> bool:
    async with read_tx(w.sm) as s:
        return START in await slots.get_free_slots(
            s, barber_id=w.ali_id, service_id=w.svc30, day=D(1), now=NOW
        )


async def test_pending_to_confirmed(world: World) -> None:
    bid, _ = await _pending(world)
    assert await _cells(world) == 2
    async with write_tx(world.sm) as s:
        b = await bookings.confirm_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=NOW
        )
    assert b.status == BookingStatus.CONFIRMED and b.awaiting is None
    assert await _cells(world) == 2
    # idempotent repeat
    async with write_tx(world.sm) as s:
        b2 = await bookings.confirm_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=NOW
        )
    assert b2 == b


async def test_admin_can_decide(world: World) -> None:
    bid, _ = await _pending(world)
    async with write_tx(world.sm) as s:
        b = await bookings.confirm_booking(s, booking_id=bid, actor_user_id=world.admin_id, now=NOW)
    assert b.status == BookingStatus.CONFIRMED


async def test_pending_to_rejected_frees_slot(world: World) -> None:
    bid, _ = await _pending(world)
    assert not await _slot_free(world)
    async with write_tx(world.sm) as s:
        b = await bookings.reject_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, reason="sick", now=NOW
        )
    assert b.status == BookingStatus.REJECTED and b.cancel_reason == "sick"
    assert await _cells(world) == 0 and await _slot_free(world)
    async with write_tx(world.sm) as s:  # idempotent
        await bookings.reject_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, reason=None, now=NOW
        )


async def test_cancel_by_client_frees_slot(world: World) -> None:
    bid, c = await _confirmed(world)
    async with write_tx(world.sm) as s:
        r = await bookings.cancel_by_client(s, booking_id=bid, client_user_id=c, now=NOW)
    assert r.booking.status == BookingStatus.CANCELLED_BY_CLIENT
    assert r.booking.cancelled_by == CancelledBy.CLIENT and not r.late
    assert await _cells(world) == 0 and await _slot_free(world)
    async with write_tx(world.sm) as s:  # idempotent
        r2 = await bookings.cancel_by_client(s, booking_id=bid, client_user_id=c, now=NOW)
    assert r2.booking.status == BookingStatus.CANCELLED_BY_CLIENT


async def test_cancel_by_staff_requires_reason(world: World) -> None:
    bid, _ = await _confirmed(world)
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await bookings.cancel_by_staff(
                s, booking_id=bid, actor_user_id=world.ali_user_id, reason="  ", now=NOW
            )
    async with write_tx(world.sm) as s:
        b = await bookings.cancel_by_staff(
            s, booking_id=bid, actor_user_id=world.ali_user_id, reason="kasal", now=NOW
        )
    assert b.status == BookingStatus.CANCELLED_BY_BARBER and b.cancelled_by == CancelledBy.BARBER
    assert await _cells(world) == 0


async def test_cancel_by_admin_marks_admin(world: World) -> None:
    bid, _ = await _confirmed(world)
    async with write_tx(world.sm) as s:
        b = await bookings.cancel_by_staff(
            s, booking_id=bid, actor_user_id=world.admin_id, reason="x", now=NOW
        )
    assert b.cancelled_by == CancelledBy.ADMIN


async def test_complete(world: World) -> None:
    bid, _ = await _confirmed(world)
    async with write_tx(world.sm) as s:
        with pytest.raises(TooEarlyError):
            await bookings.complete_booking(
                s,
                booking_id=bid,
                actor_user_id=world.ali_user_id,
                now=START - timedelta(minutes=31),
            )
    async with write_tx(world.sm) as s:
        r = await bookings.complete_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=START - timedelta(minutes=30)
        )
    assert r.booking.status == BookingStatus.COMPLETED and r.booking.completed_at is not None
    assert await _cells(world) == 0
    async with write_tx(world.sm) as s:
        again = await bookings.complete_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=START
        )
    assert again.earned == 0 and again.booking.status == BookingStatus.COMPLETED


async def test_no_show(world: World) -> None:
    bid, c = await _confirmed(world)
    async with write_tx(world.sm) as s:
        with pytest.raises(TooEarlyError):
            await bookings.mark_no_show(
                s,
                booking_id=bid,
                actor_user_id=world.ali_user_id,
                now=START + timedelta(minutes=14),
            )
    async with write_tx(world.sm) as s:
        r = await bookings.mark_no_show(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=START + timedelta(minutes=15)
        )
    assert r.booking.status == BookingStatus.NO_SHOW
    assert await _cells(world) == 0
    async with write_tx(world.sm) as s:  # idempotent, counter not doubled
        await bookings.mark_no_show(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=START + timedelta(minutes=20)
        )
    from app.services import users

    async with read_tx(world.sm) as s:
        assert (await users.get_user(s, c)).no_show_count == 1


ACTIONS = ("confirm", "reject", "complete", "no_show", "cancel_client", "cancel_staff", "propose")


async def _do(w: World, action: str, bid: int, c: int, now):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        if action == "confirm":
            return await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=w.ali_user_id, now=now
            )
        if action == "reject":
            return await bookings.reject_booking(
                s, booking_id=bid, actor_user_id=w.ali_user_id, reason=None, now=now
            )
        if action == "complete":
            return await bookings.complete_booking(
                s, booking_id=bid, actor_user_id=w.ali_user_id, now=now
            )
        if action == "no_show":
            return await bookings.mark_no_show(
                s, booking_id=bid, actor_user_id=w.ali_user_id, now=now
            )
        if action == "cancel_client":
            return await bookings.cancel_by_client(s, booking_id=bid, client_user_id=c, now=now)
        if action == "cancel_staff":
            return await bookings.cancel_by_staff(
                s, booking_id=bid, actor_user_id=w.ali_user_id, reason="r", now=now
            )
        if action == "propose":
            return await bookings.propose_time(
                s,
                booking_id=bid,
                actor_user_id=w.ali_user_id,
                new_start_at=L(D(1), "15:00"),
                now=now,
            )
    raise AssertionError(action)


# (setup, action) pairs that must raise InvalidTransitionError
FORBIDDEN = [
    ("confirmed", "reject"),
    ("confirmed", "propose"),
    ("pending", "complete"),
    ("pending", "no_show"),
    ("rejected", "confirm"),
    ("rejected", "complete"),
    ("rejected", "cancel_client"),
    ("rejected", "cancel_staff"),
    ("cancelled", "confirm"),
    ("cancelled", "reject"),
    ("cancelled", "complete"),
    ("cancelled", "no_show"),
    ("completed", "confirm"),
    ("completed", "reject"),
    ("completed", "cancel_client"),
    ("completed", "cancel_staff"),
    ("completed", "no_show"),  # manual completion (not auto) cannot flip
    ("no_show", "complete"),
    ("no_show", "confirm"),
]


@pytest.mark.parametrize("setup,action", FORBIDDEN)
async def test_forbidden_transitions(world: World, setup: str, action: str) -> None:
    if setup == "pending":
        bid, c = await _pending(world)
    else:
        bid, c = await _confirmed(world)
    later = START + timedelta(minutes=20)
    if setup == "rejected":
        await world.set("new_client_requires_approval", "true")
        c = await world.new_client()
        r = await book(world, c, L(D(1), "16:00"))
        bid = r.booking.id
        await _do(world, "reject", bid, c, NOW)
    elif setup == "cancelled":
        await _do(world, "cancel_client", bid, c, NOW)
    elif setup == "completed":
        await _do(world, "complete", bid, c, START)
    elif setup == "no_show":
        await _do(world, "no_show", bid, c, later)
    cells_before = await _cells(world)
    with pytest.raises((InvalidTransitionError, TooLateError)) as e:
        await _do(world, action, bid, c, NOW if setup in ("pending", "confirmed") else later)
    if setup != "completed" or action != "cancel_client":
        assert isinstance(e.value, InvalidTransitionError), e.value
    assert e.value.ctx.get("current") or isinstance(e.value, TooLateError)
    assert await _cells(world) == cells_before


async def test_cells_invariant_active_only(world: World) -> None:
    from sqlalchemy import select

    from app.db.models import Booking

    bid, c = await _confirmed(world)
    await _do(world, "complete", bid, c, START)
    async with read_tx(world.sm) as s:
        rows = (await s.execute(select(Booking))).scalars().all()
        for b in rows:
            n = (
                (await s.execute(select(BookingSlot).where(BookingSlot.booking_id == b.id)))
                .scalars()
                .all()
            )
            if b.status in ACTIVE_STATUSES:
                assert len(n) > 0
            else:
                assert n == []


async def test_permissions_on_transitions(world: World) -> None:
    bek, bek_user = await world.new_barber("Bekzod")
    bid, c = await _pending(world)
    other = await world.new_client()
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.confirm_booking(s, booking_id=bid, actor_user_id=bek_user, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.confirm_booking(s, booking_id=bid, actor_user_id=c, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.cancel_by_client(s, booking_id=bid, client_user_id=other, now=NOW)


# ------------------------------------------------------------ late accept [B5]


async def test_late_accept_within_grace(world: World) -> None:
    bid, _ = await _pending(world)
    async with read_tx(world.sm) as s:
        exp = (await bookings.get_booking(s, bid)).expire_at
    async with write_tx(world.sm) as s:
        expired = await bookings.expire_due_pending(s, now=exp)
    assert [b.id for b in expired] == [bid] and expired[0].status == BookingStatus.EXPIRED
    assert await _cells(world) == 0
    async with write_tx(world.sm) as s:
        b = await bookings.confirm_booking(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=exp + timedelta(minutes=5)
        )
    assert b.status == BookingStatus.CONFIRMED and b.cancelled_by is None
    assert await _cells(world) == 2


async def test_late_accept_outside_grace(world: World) -> None:
    bid, _ = await _pending(world)
    async with read_tx(world.sm) as s:
        exp = (await bookings.get_booking(s, bid)).expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    async with write_tx(world.sm) as s:
        with pytest.raises(TooLateError):
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.ali_user_id, now=exp + timedelta(minutes=11)
            )


async def test_late_accept_slot_taken(world: World) -> None:
    bid, _ = await _pending(world)
    async with read_tx(world.sm) as s:
        exp = (await bookings.get_booking(s, bid)).expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    await world.set("new_client_requires_approval", "false")
    c2 = await world.new_client()
    await book(world, c2, START, now=exp)
    async with write_tx(world.sm) as s:
        with pytest.raises(SlotTakenError):
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.ali_user_id, now=exp + timedelta(minutes=1)
            )


async def test_confirm_after_start_too_late(world: World) -> None:
    bid, _ = await _pending(world)
    async with write_tx(world.sm) as s:
        with pytest.raises((TooLateError, InvalidTransitionError)):
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.ali_user_id, now=START + timedelta(minutes=1)
            )


async def test_awaiting_client_cannot_be_confirmed_by_barber(world: World) -> None:
    bid, _ = await _pending(world)
    await _do(world, "propose", bid, 0, NOW)
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, bid)).awaiting == Awaiting.CLIENT
    with pytest.raises(InvalidTransitionError):
        await _do(world, "confirm", bid, 0, NOW)
    with pytest.raises(InvalidTransitionError):
        await _do(world, "reject", bid, 0, NOW)
