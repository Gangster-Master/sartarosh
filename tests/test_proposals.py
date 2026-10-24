from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.enums import Awaiting, BookingStatus
from app.db.models import BookingSlot
from app.db.uow import read_tx, write_tx
from app.services import bookings
from app.services.errors import (
    PermissionDeniedError,
    ProposalLimitError,
    SlotTakenError,
    SlotUnavailableError,
    TooLateError,
)
from tests.factories import NOW, D, L, World, book


async def _pending(w: World) -> tuple[int, int]:
    await w.set("new_client_requires_approval", "true")
    c = await w.new_client()
    r = await book(w, c, L(D(1), "10:30"))
    return r.booking.id, c


async def _propose(w: World, bid: int, hhmm: str, now=NOW):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await bookings.propose_time(
            s, booking_id=bid, actor_user_id=w.ali_user_id, new_start_at=L(D(1), hhmm), now=now
        )


async def _cells(w: World, bid: int) -> list:  # type: ignore[type-arg]
    async with read_tx(w.sm) as s:
        return list(
            (
                await s.execute(select(BookingSlot.slot_start).where(BookingSlot.booking_id == bid))
            ).scalars()
        )


async def test_propose_moves_cells(world: World) -> None:
    bid, _ = await _pending(world)
    r = await _propose(world, bid, "11:00")
    b = r.booking
    assert b.status == BookingStatus.PENDING and b.awaiting == Awaiting.CLIENT
    assert b.start_at == L(D(1), "11:00") and b.original_start_at == L(D(1), "10:30")
    assert b.proposal_count == 1
    assert b.expire_at == NOW + timedelta(minutes=30)  # proposal_reply_min
    assert sorted(await _cells(world, bid)) == [L(D(1), "11:00"), L(D(1), "11:15")]
    # idempotent repeat of the same proposal
    r2 = await _propose(world, bid, "11:00")
    assert r2.booking.proposal_count == 1


async def test_accept_proposal(world: World) -> None:
    bid, c = await _pending(world)
    await _propose(world, bid, "11:00")
    async with write_tx(world.sm) as s:
        b = await bookings.answer_proposal(
            s, booking_id=bid, client_user_id=c, accept=True, now=NOW
        )
    assert b.status == BookingStatus.CONFIRMED and b.start_at == L(D(1), "11:00")
    # 10:30 is free again
    c2 = await world.new_client()
    await world.set("new_client_requires_approval", "false")
    r = await book(world, c2, L(D(1), "10:30"))
    assert r.booking.status == BookingStatus.CONFIRMED
    async with write_tx(world.sm) as s:  # idempotent
        await bookings.answer_proposal(s, booking_id=bid, client_user_id=c, accept=True, now=NOW)


async def test_decline_proposal_not_late(world: World) -> None:
    bid, c = await _pending(world)
    await _propose(world, bid, "11:00")
    async with write_tx(world.sm) as s:
        b = await bookings.answer_proposal(
            s, booking_id=bid, client_user_id=c, accept=False, now=L(D(1), "10:00")
        )
    assert b.status == BookingStatus.CANCELLED_BY_CLIENT
    assert b.cancel_reason == "proposal_declined" and not b.late_cancel
    assert await _cells(world, bid) == []
    from app.services import users

    async with read_tx(world.sm) as s:
        assert (await users.get_user(s, c)).late_cancel_count == 0


async def test_third_proposal_refused(world: World) -> None:
    bid, c = await _pending(world)
    # The state machine only allows a proposal while awaiting BARBER. To reach a
    # second proposal the flow must come back to the barber; that path does not
    # exist, so proposal_count=2 is reachable only via the DB. Simulate it.
    from app.db.models import Booking

    async with write_tx(world.sm) as s:
        row = await s.get(Booking, bid)
        row.proposal_count = 2
    with pytest.raises(ProposalLimitError):
        await _propose(world, bid, "11:00")


async def test_propose_to_taken_or_invalid(world: World) -> None:
    bid, _ = await _pending(world)
    await world.set("new_client_requires_approval", "false")
    c2 = await world.new_client()
    await book(world, c2, L(D(1), "12:00"))
    with pytest.raises(SlotTakenError):
        await _propose(world, bid, "12:00")
    with pytest.raises(SlotUnavailableError):
        await _propose(world, bid, "13:00")  # break
    with pytest.raises(SlotUnavailableError):
        await _propose(world, bid, "10:30")  # same time


async def test_client_reply_expiry(world: World) -> None:
    bid, c = await _pending(world)
    r = await _propose(world, bid, "11:00")
    exp = r.booking.expire_at
    async with write_tx(world.sm) as s:
        with pytest.raises(TooLateError):
            await bookings.answer_proposal(
                s, booking_id=bid, client_user_id=c, accept=True, now=exp
            )
    async with write_tx(world.sm) as s:
        out = await bookings.expire_due_pending(s, now=exp)
    assert [b.id for b in out] == [bid]
    assert await _cells(world, bid) == []
    # a proposal that expired can't be late-accepted by the barber
    from app.services.errors import InvalidTransitionError

    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidTransitionError):
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.ali_user_id, now=exp + timedelta(minutes=1)
            )


async def test_other_client_cannot_answer(world: World) -> None:
    bid, _ = await _pending(world)
    await _propose(world, bid, "11:00")
    other = await world.new_client()
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.answer_proposal(
                s, booking_id=bid, client_user_id=other, accept=True, now=NOW
            )


async def test_proposal_expiry_respects_cutoff_and_min_decision(world: World) -> None:
    bid, _ = await _pending(world)
    # proposing 10:45 tomorrow at a time where cutoff (start-10) dominates
    now = L(D(1), "10:00")
    r = await _propose(world, bid, "11:00", now=now - timedelta(minutes=29))  # 09:31
    # min(09:31+30=10:01, 10:50) = 10:01 ; max(.., 09:46) = 10:01
    assert r.booking.expire_at == now + timedelta(minutes=1)
