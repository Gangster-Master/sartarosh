from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete

from app.db.models import Booking, BookingSlot
from app.db.uow import write_tx
from app.services import bookings, slots
from app.services.errors import ServiceError, SlotTakenError
from tests.factories import D, L, World, book, count_rows

ITERATIONS = 20


async def _wipe(w: World) -> None:
    async with write_tx(w.sm) as s:
        await s.execute(delete(BookingSlot))
        await s.execute(delete(Booking))


async def _race(w: World, a: tuple, b: tuple) -> list[object]:  # type: ignore[type-arg]
    async def attempt(client: int, start, svc):  # type: ignore[no-untyped-def]
        try:
            return await book(w, client, start, service_id=svc)
        except ServiceError as e:
            return e

    return await asyncio.gather(attempt(*a), attempt(*b))


@pytest.mark.parametrize(
    "first,second",
    [
        (("10:00", "30"), ("10:00", "30")),  # same slot
        (("10:00", "60"), ("10:30", "30")),  # overlap
    ],
)
async def test_race_exactly_one_wins(file_world: World, first, second) -> None:  # type: ignore[no-untyped-def]
    w = file_world
    c1, c2 = await w.new_client(), await w.new_client()
    for _ in range(ITERATIONS):
        await _wipe(w)
        res = await _race(
            w,
            (c1, L(D(1), first[0]), first[1]),
            (c2, L(D(1), second[0]), second[1]),
        )
        wins = [r for r in res if not isinstance(r, Exception)]
        losses = [r for r in res if isinstance(r, Exception)]
        assert len(wins) == 1, res
        assert len(losses) == 1 and isinstance(losses[0], SlotTakenError), res
        assert await count_rows(w.sm, Booking) == 1
        win = wins[0].booking  # type: ignore[attr-defined]
        assert await count_rows(w.sm, BookingSlot) == win.duration_min // 15


async def test_cell_guard_leaves_no_orphan_rows(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the pre-check is bypassed, the booking_slots PK stops a double booking
    and the loser's booking row is rolled back with its SAVEPOINT [A1]."""
    c1, c2 = await world.new_client(), await world.new_client()
    await book(world, c1, L(D(1), "10:00"))
    before_b, before_c = (
        await count_rows(world.sm, Booking),
        await count_rows(world.sm, BookingSlot),
    )

    async def blind(*a, **k):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(bookings, "slot_problem", blind)
    async with write_tx(world.sm) as s:
        with pytest.raises(SlotTakenError):
            await bookings.create_booking(
                s,
                client_user_id=c2,
                barber_id=world.ali_id,
                service_id=world.svc30,
                start_at=L(D(1), "10:00"),
                now=L(D(0), "09:00"),
            )
        # inside the same transaction: nothing left behind
        from sqlalchemy import func, select

        assert (await s.execute(select(func.count()).select_from(Booking))).scalar_one() == before_b
        assert (
            await s.execute(select(func.count()).select_from(BookingSlot))
        ).scalar_one() == before_c
    assert slots  # imported for monkeypatch target clarity
