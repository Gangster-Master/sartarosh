from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.uow import read_tx, write_tx
from app.services import (
    barbers,
    bonus,
    bookings,
    catalog,
    schedule,
    settings,
    slots,
    stats,
    users,
)
from app.services.errors import PermissionDeniedError
from tests.factories import NOW, D, L, World, book


async def test_barber_b_cannot_read_or_act_on_alis_booking(world: World) -> None:
    _, bek_user = await world.new_barber("Bekzod")
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.get_booking_for_actor(s, booking_id=r.booking.id, actor_user_id=bek_user)
    async with read_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await slots.list_proposal_slots(s, booking_id=r.booking.id, actor_user_id=bek_user)
    async with read_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.list_barber_day(
                s, actor_user_id=bek_user, barber_id=world.ali_id, day=D(1)
            )
    for fn in (bookings.complete_booking, bookings.mark_no_show):
        async with write_tx(world.sm) as s:
            with pytest.raises(PermissionDeniedError):
                await fn(s, booking_id=r.booking.id, actor_user_id=bek_user, now=NOW)


async def test_readers(world: World) -> None:
    c = await world.new_client()
    other = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        assert (
            await bookings.get_booking_for_actor(s, booking_id=r.booking.id, actor_user_id=c)
        ).id
        assert await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id
        )
        assert await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=world.admin_id
        )
        with pytest.raises(PermissionDeniedError):
            await bookings.get_booking_for_actor(s, booking_id=r.booking.id, actor_user_id=other)


async def test_barber_sees_masked_phone_on_old_bookings(world: World) -> None:
    c = await world.new_client(phone="+998901234567")
    r = await book(world, c, L(D(0), "10:00"))
    later = L(D(2), "09:00")
    async with read_tx(world.sm) as s:
        b_bar = await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=later
        )
        b_adm = await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=world.admin_id, now=later
        )
        b_cli = await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=c, now=later
        )
        b_bar_fresh = await bookings.get_booking_for_actor(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(1), "09:00")
        )
        day = await bookings.list_barber_day(
            s, actor_user_id=world.ali_user_id, barber_id=world.ali_id, day=D(0), now=later
        )
    assert b_bar.client_phone == "+998 90 *** ** 67"
    assert b_adm.client_phone == b_cli.client_phone == "+998901234567"
    assert b_bar_fresh.client_phone == "+998901234567"
    assert day[0].client_phone == "+998 90 *** ** 67"


async def test_non_admin_refused_by_admin_services(world: World) -> None:
    c = await world.new_client()
    calls = [
        lambda s: bookings.list_bookings(s, actor_user_id=c),
        lambda s: users.find_users_by_phone(s, actor_user_id=c, phone_query="1234"),
        lambda s: users.get_client_profile(s, actor_user_id=c, user_id=c),
        lambda s: users.set_blocked(s, actor_user_id=c, user_id=world.ali_user_id, blocked=True),
        lambda s: users.reset_no_shows(s, actor_user_id=c, user_id=c),
        lambda s: users.set_admin(s, actor_user_id=c, user_id=c, is_admin=True),
        lambda s: users.list_admins(s, actor_user_id=c),
        lambda s: barbers.create_barber(s, actor_user_id=c, display_name="X"),
        lambda s: barbers.update_barber(s, actor_user_id=c, barber_id=world.ali_id, bio="x"),
        lambda s: barbers.deactivate_barber(s, actor_user_id=c, barber_id=world.ali_id, reason="x"),
        lambda s: barbers.preview_future_bookings(s, actor_user_id=c, barber_id=world.ali_id),
        lambda s: barbers.regenerate_invite(s, actor_user_id=c, barber_id=world.ali_id),
        lambda s: barbers.unlink_barber(s, actor_user_id=c, barber_id=world.ali_id),
        lambda s: catalog.create_service(
            s, actor_user_id=c, name="X", duration_min=30, price_uzs=1
        ),
        lambda s: catalog.update_service(s, actor_user_id=c, service_id=world.svc30, price_uzs=1),
        lambda s: catalog.set_barber_services(
            s, actor_user_id=c, barber_id=world.ali_id, service_ids=[]
        ),
        lambda s: schedule.set_working_hours(
            s, actor_user_id=c, barber_id=world.ali_id, weekday=0, start_time=None, end_time=None
        ),
        lambda s: schedule.add_time_off(
            s,
            actor_user_id=c,
            barber_id=None,
            start_at=NOW,
            end_at=NOW + timedelta(hours=1),
            reason=None,
            cancel_conflicts=False,
        ),
        lambda s: settings.update_setting(s, actor_user_id=c, key="min_lead_min", raw_value="60"),
        lambda s: settings.set_location(s, actor_user_id=c, latitude=41.3, longitude=69.2),
        lambda s: bonus.adjust_bonus(s, actor_user_id=c, user_id=c, delta=5, comment="x"),
        lambda s: stats.get_stats(s, actor_user_id=c, start=D(0), end=D(0)),
    ]
    for i, call in enumerate(calls):
        async with write_tx(world.sm) as s:
            with pytest.raises(PermissionDeniedError):
                await call(s)
        assert i >= 0


async def test_barber_is_not_admin(world: World) -> None:
    async with read_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.list_bookings(s, actor_user_id=world.ali_user_id)


async def test_client_cannot_ack_or_answer_others(world: World) -> None:
    c, other = await world.new_client(), await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bookings.client_ack(s, booking_id=r.booking.id, client_user_id=other, now=NOW)
