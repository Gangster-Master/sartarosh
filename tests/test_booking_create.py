from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.enums import Awaiting, BookingSource, BookingStatus
from app.db.models import Booking, BookingSlot
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, users
from app.services.errors import (
    BarberInactiveError,
    ConsentRequiredError,
    InputError,
    InvalidNameError,
    PhoneRequiredError,
    SlotTakenError,
    SlotUnavailableError,
    UserBlockedError,
)
from tests.factories import ENV_ADMIN_TG, NOW, D, L, World, book, count_rows, new_client


async def test_happy_path_auto_confirm(world: World) -> None:
    c = await world.new_client(name="Gʻayrat")
    r = await book(world, c, L(D(1), "10:30"), comment="<b>soqol</b>")
    b = r.booking
    assert r.auto_confirmed and b.status == BookingStatus.CONFIRMED and b.awaiting is None
    assert r.decision_targets == []
    assert b.source == BookingSource.BOT
    assert b.client_name == "Gʻayrat"
    assert b.service_name == "Soch olish" and b.price_snapshot == 60_000
    assert b.final_price_uzs == 60_000 and b.duration_min == 30
    assert b.comment == "<b>soqol</b>"
    assert b.barber_name == "Ali" and b.client_phone_verified
    assert b.end_at - b.start_at == L(D(1), "11:00") - L(D(1), "10:30")
    assert await count_rows(world.sm, BookingSlot) == 2


async def test_block_end_snapshot_includes_buffer(world: World) -> None:
    async with write_tx(world.sm) as s:
        await barbers.update_barber(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, buffer_min=15
        )
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        row = await s.get(Booking, r.booking.id)
        assert row.block_end_at == L(D(1), "10:45")
        cells = (await s.execute(select(BookingSlot.slot_start))).scalars().all()
    assert len(cells) == 3


async def test_phone_required(world: World) -> None:
    c = await new_client(world.sm, verified=False)
    with pytest.raises(PhoneRequiredError):
        await book(world, c, L(D(1), "10:00"))


async def test_consent_required(world: World) -> None:
    c = await world.new_client(consent=False)
    with pytest.raises(ConsentRequiredError):
        await book(world, c, L(D(1), "10:00"))


async def test_invalid_display_name_asks_for_name(world: World) -> None:
    c = await world.new_client(name="<b>x</b>")
    with pytest.raises(InvalidNameError):
        await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        await users.set_full_name(s, user_id=c, name="Ali Vali")
    r = await book(world, c, L(D(1), "10:00"))
    assert r.booking.client_name == "Ali Vali"


async def test_blocked_user(world: World) -> None:
    c = await world.new_client()
    async with write_tx(world.sm) as s:
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=c, blocked=True, now=NOW)
    with pytest.raises(UserBlockedError):
        await book(world, c, L(D(1), "10:00"))


async def test_new_client_pending_with_targets(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(0), "15:00"), now=L(D(0), "11:00"))
    b = r.booking
    assert b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER
    assert not r.auto_confirmed
    assert b.expire_at == L(D(0), "12:00")
    roles = {t.role for t in r.decision_targets}
    chats = {t.chat_id for t in r.decision_targets}
    # Ali has auto-accept on but the booking needed approval -> admins too [#23]
    assert roles == {"barber", "admin"} and 2000 in chats and ENV_ADMIN_TG in chats


async def test_pending_without_auto_accept_only_barber(world: World) -> None:
    bek, _ = await world.new_barber("Bekzod", auto_accept=False)
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"), barber_id=bek)
    assert r.booking.status == BookingStatus.PENDING
    assert [t.role for t in r.decision_targets] == ["barber"]


async def test_unreachable_barber_goes_to_admins(world: World) -> None:
    async with write_tx(world.sm) as s:
        await barbers.set_reachable(s, barber_id=world.ali_id, reachable=False)
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    assert r.booking.status == BookingStatus.PENDING
    assert [t.role for t in r.decision_targets] == ["admin"]
    async with write_tx(world.sm) as s:
        assert await barbers.mark_reachable_by_user(s, user_id=world.ali_user_id) is True
        assert await barbers.mark_reachable_by_user(s, user_id=world.ali_user_id) is False


async def test_returning_client_confirmed_even_with_approval(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(0), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.confirm_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=NOW
        )
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "10:30")
        )
    r2 = await book(world, c, L(D(1), "10:00"), now=L(D(0), "11:00"))
    assert r2.auto_confirmed


async def test_invalid_times(world: World) -> None:
    c = await world.new_client()
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(1), "10:10"))  # off grid
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(1), "13:00"))  # break
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(6), "10:00"))  # Sunday
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(7), "10:00"))  # past horizon
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(0), "09:15"))  # inside lead time
    with pytest.raises(SlotUnavailableError):
        await book(world, c, L(D(1), "19:30"), service_id="60")  # past close
    from datetime import datetime

    with pytest.raises(SlotUnavailableError):
        await book(world, c, datetime(2026, 10, 27, 10, 0))  # naive
    assert await count_rows(world.sm, Booking) == 0


async def test_slot_taken(world: World) -> None:
    c1, c2 = await world.new_client(), await world.new_client()
    await book(world, c1, L(D(1), "10:00"), service_id="60")
    with pytest.raises(SlotTakenError):
        await book(world, c2, L(D(1), "10:30"))
    assert await count_rows(world.sm, Booking) == 1


async def test_unlinked_or_inactive_barber(world: World) -> None:
    c = await world.new_client()
    async with write_tx(world.sm) as s:
        ghost = await barbers.create_barber(s, actor_user_id=world.admin_id, display_name="Ghost")
    with pytest.raises(BarberInactiveError):
        await book(world, c, L(D(1), "10:00"), barber_id=ghost.id)


async def test_service_not_offered(world: World) -> None:
    from app.services import catalog

    bek, _ = await world.new_barber("Bekzod")
    async with write_tx(world.sm) as s:
        await catalog.set_barber_services(
            s, actor_user_id=world.admin_id, barber_id=bek, service_ids=[world.svc30]
        )
    c = await world.new_client()
    with pytest.raises(InputError):
        await book(world, c, L(D(1), "10:00"), barber_id=bek, service_id="60")


async def test_any_barber_picks_least_loaded(world: World) -> None:
    bek, _ = await world.new_barber("Bekzod")
    c1 = await world.new_client()
    c2 = await world.new_client()
    c3 = await world.new_client()
    # Ali has one booking that day -> Bekzod is least loaded
    await book(world, c1, L(D(1), "15:00"))
    r = await book(world, c2, L(D(1), "10:00"), barber_id=None)
    assert r.booking.barber_id == bek and r.booking.barber_name == "Bekzod"
    # equal load (1 each) -> sort_order: Ali first
    r = await book(world, c3, L(D(1), "11:00"), barber_id=None)
    assert r.booking.barber_id == world.ali_id


async def test_any_barber_falls_through_on_slot_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the first candidate's cells are taken at insert time, the next barber gets it."""
    bek, _ = await world.new_barber("Bekzod")
    c1, c2 = await world.new_client(), await world.new_client()
    await book(world, c1, L(D(1), "15:00"), barber_id=bek)  # Bekzod busier -> Ali first

    real_occupy = bookings._occupy
    calls: list[int] = []

    async def flaky_occupy(session, booking):  # type: ignore[no-untyped-def]
        calls.append(booking.barber_id)
        if booking.barber_id == world.ali_id:
            raise SlotTakenError("simulated race")
        await real_occupy(session, booking)

    monkeypatch.setattr(bookings, "_occupy", flaky_occupy)
    r = await book(world, c2, L(D(1), "10:00"), barber_id=None)
    assert calls == [world.ali_id, bek]
    assert r.booking.barber_id == bek
    assert await count_rows(world.sm, Booking) == 2


async def test_any_barber_none_free(world: World) -> None:
    c1, c2 = await world.new_client(), await world.new_client()
    await book(world, c1, L(D(1), "10:00"))
    with pytest.raises(SlotTakenError):
        await book(world, c2, L(D(1), "10:00"), barber_id=None)


async def test_no_service_uses_step(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"), service_id=None)
    assert r.booking.service_id is None and r.booking.duration_min == 30
    assert r.booking.price_snapshot == 0
