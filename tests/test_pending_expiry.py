from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.enums import BookingStatus, CancelledBy
from app.db.uow import read_tx, write_tx
from app.services import bookings, settings
from app.services.errors import InputError
from tests.factories import D, L, World, book

# D(-1) is Sunday 25.10, D(0) Monday 26.10
CASES = [
    ("a", L(D(0), "11:00"), L(D(0), "15:00"), L(D(0), "12:00")),
    ("b", L(D(-1), "23:00"), L(D(0), "10:00"), L(D(0), "09:50")),
    ("c", L(D(-1), "23:00"), L(D(0), "12:00"), L(D(0), "11:00")),
    ("d", L(D(0), "09:00"), L(D(0), "10:30"), L(D(0), "10:20")),
    ("e", L(D(0), "14:25"), L(D(0), "15:00"), L(D(0), "14:50")),
]


@pytest.mark.parametrize("name,created,start,expected", CASES)
async def test_compute_expire_at(world: World, name, created, start, expected) -> None:  # type: ignore[no-untyped-def]
    async with read_tx(world.sm) as s:
        got = await bookings.compute_expire_at(
            s, barber_id=world.ali_id, created_at=created, start_at=start
        )
    assert got == expected, name


async def test_created_in_break_waits_for_window(world: World) -> None:
    async with read_tx(world.sm) as s:
        got = await bookings.compute_expire_at(
            s, barber_id=world.ali_id, created_at=L(D(0), "13:10"), start_at=L(D(0), "18:00")
        )
    assert got == L(D(0), "15:00")  # base 14:00 + 60


async def test_min_lead_cross_field_rule(world: World) -> None:
    with pytest.raises(InputError) as e:
        await world.set("min_lead_min", "20")
    assert e.value.ctx["field"] == "min_lead_min"
    await world.set("min_lead_min", "25")
    with pytest.raises(InputError):
        await world.set("min_decision_min", "20")  # 10 + 20 > 25


async def test_expire_due_pending(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    created = L(D(0), "11:00")
    r = await book(world, c, L(D(0), "15:00"), now=created)
    assert r.booking.expire_at == L(D(0), "12:00")
    async with write_tx(world.sm) as s:
        assert await bookings.expire_due_pending(s, now=L(D(0), "11:59")) == []
    async with write_tx(world.sm) as s:
        out = await bookings.expire_due_pending(s, now=L(D(0), "12:00"))
    assert len(out) == 1 and out[0].status == BookingStatus.EXPIRED
    assert out[0].cancelled_by == CancelledBy.SYSTEM
    async with write_tx(world.sm) as s:  # second run finds nothing
        assert await bookings.expire_due_pending(s, now=L(D(0), "12:30")) == []


async def test_min_decision_floor(world: World) -> None:
    """A request made 35 min ahead during hours gets >= 15 min [B2]."""
    async with read_tx(world.sm) as s:
        got = await bookings.compute_expire_at(
            s, barber_id=world.ali_id, created_at=L(D(0), "14:25"), start_at=L(D(0), "15:00")
        )
        st = await settings.get_shop_settings(s)
    assert got - L(D(0), "14:25") >= timedelta(minutes=st.min_decision_min)
