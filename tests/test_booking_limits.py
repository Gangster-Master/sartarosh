from __future__ import annotations

import pytest

from app.db.uow import write_tx
from app.services import bookings
from app.services.errors import ClientLimitError
from tests.factories import NOW, D, L, World, book


async def test_max_active(world: World) -> None:
    c = await world.new_client()
    for i, t in enumerate(("10:00", "11:00", "12:00")):
        await book(world, c, L(D(1 + i), t))
    with pytest.raises(ClientLimitError) as e:
        await book(world, c, L(D(4), "10:00"))
    assert e.value.ctx["kind"] == "max_active" and e.value.ctx["limit"] == 3


async def test_max_active_counted_per_phone(world: World) -> None:
    phone = "+998901112233"
    c1 = await world.new_client(phone=phone)
    c2 = await world.new_client(phone=phone)  # second account, same verified phone [D1]
    await book(world, c1, L(D(1), "10:00"))
    await book(world, c1, L(D(2), "10:00"))
    await book(world, c2, L(D(3), "10:00"))
    with pytest.raises(ClientLimitError) as e:
        await book(world, c2, L(D(4), "10:00"))
    assert e.value.ctx["kind"] == "max_active"


async def test_max_per_day_counts_cancelled(world: World) -> None:
    await world.set("max_requests_per_day", "2")
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.cancel_by_client(s, booking_id=r.booking.id, client_user_id=c, now=NOW)
    await book(world, c, L(D(1), "11:00"))
    with pytest.raises(ClientLimitError) as e:
        await book(world, c, L(D(1), "12:00"))
    assert e.value.ctx["kind"] == "max_per_day"
    # next local day the counter resets
    await book(world, c, L(D(2), "12:00"), now=L(D(1), "08:00"))


async def test_overlap_across_barbers_refused_adjacent_allowed(world: World) -> None:
    bek, _ = await world.new_barber("Bekzod")
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    with pytest.raises(ClientLimitError) as e:
        await book(world, c, L(D(1), "10:00"), barber_id=bek)
    assert e.value.ctx["kind"] == "overlap"
    # back to back with the same barber is fine [E5]
    r = await book(world, c, L(D(1), "10:30"))
    assert r.booking.status.value == "CONFIRMED"


async def test_past_active_booking_does_not_count(world: World) -> None:
    c = await world.new_client()
    for t in ("10:00", "11:00", "12:00"):
        await book(world, c, L(D(0), t))
    # an hour after the last one ended they are no longer "active future"
    await book(world, c, L(D(1), "10:00"), now=L(D(0), "13:30"))
