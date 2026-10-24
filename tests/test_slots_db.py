from __future__ import annotations

from app.db.uow import read_tx, write_tx
from app.services import barbers, clock, slots
from tests.factories import NOW, D, L, World, book


def hm(xs):  # type: ignore[no-untyped-def]
    return [clock.to_local(x).strftime("%H:%M") for x in xs]


async def test_wrapper_today_respects_lead(world: World) -> None:
    async with read_tx(world.sm) as s:
        out = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=D(0), now=NOW
        )
    assert hm(out)[0] == "10:00"  # 09:00 + 30 lead
    async with read_tx(world.sm) as s:
        out = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=D(0), now=L(D(0), "10:05")
        )
    assert hm(out)[0] == "11:00"  # 10:35 ceil -> 10:45, step 30 -> 11:00


async def test_sunday_and_horizon(world: World) -> None:
    async with read_tx(world.sm) as s:
        assert (
            await slots.get_free_slots(
                s, barber_id=world.ali_id, service_id=world.svc30, day=D(6), now=NOW
            )
            == []
        )
        assert (
            await slots.get_free_slots(
                s, barber_id=world.ali_id, service_id=world.svc30, day=D(7), now=NOW
            )
            == []
        )  # horizon 7 days: Mon..Sun


async def test_booked_slot_excluded(world: World) -> None:
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"), service_id="60")
    async with read_tx(world.sm) as s:
        out = hm(
            await slots.get_free_slots(
                s, barber_id=world.ali_id, service_id=world.svc30, day=D(1), now=NOW
            )
        )
    assert "10:00" not in out and "10:30" not in out and "11:00" in out


async def test_any_barber_union_and_days(world: World) -> None:
    bek_id, _ = await world.new_barber("Bekzod")
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        any_ = hm(await slots.get_free_slots_any(s, service_id=world.svc30, day=D(1), now=NOW))
        ali = hm(
            await slots.get_free_slots(
                s, barber_id=world.ali_id, service_id=world.svc30, day=D(1), now=NOW
            )
        )
        days = await slots.get_available_days(s, barber_id=None, service_id=world.svc30, now=NOW)
    assert "10:00" not in ali and "10:00" in any_  # Bekzod is free at 10:00
    assert len(days) == 7
    assert days[0].day == D(0)
    sunday = days[6]
    assert sunday.is_working is False and sunday.has_free_slots is False
    assert all(d.is_working and d.has_free_slots for d in days[:6])


async def test_unlinked_barber_gives_no_slots(world: World) -> None:
    async with write_tx(world.sm) as s:
        b = await barbers.create_barber(s, actor_user_id=world.admin_id, display_name="Ghost")
        from tests.factories import set_std_hours

        await set_std_hours(s, world.admin_id, b.id)
    async with read_tx(world.sm) as s:
        assert (
            await slots.get_free_slots(s, barber_id=b.id, service_id=world.svc30, day=D(1), now=NOW)
            == []
        )
        days = await slots.get_available_days(s, barber_id=b.id, service_id=None, now=NOW)
        assert not any(d.is_working for d in days)
        pub = await barbers.list_barbers(s)
    assert [x.display_name for x in pub] == ["Ali"]


async def test_full_day_time_off_marks_day_not_working(world: World) -> None:
    from app.services import schedule

    async with write_tx(world.sm) as s:
        iv = schedule.full_day_interval(D(2))
        await schedule.add_time_off(
            s,
            actor_user_id=world.admin_id,
            barber_id=None,
            start_at=iv.start,
            end_at=iv.end,
            reason="Navro'z",
            cancel_conflicts=False,
            now=NOW,
        )
    async with read_tx(world.sm) as s:
        days = await slots.get_available_days(
            s, barber_id=world.ali_id, service_id=world.svc30, now=NOW
        )
    assert days[2].is_working is False and days[2].has_free_slots is False
    assert days[3].is_working is True


async def test_proposal_slots_ignore_own_cells(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    assert r.booking.status.value == "PENDING"
    async with read_tx(world.sm) as s:
        out = await slots.list_proposal_slots(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=NOW
        )
    assert len(out) == 8
    assert L(D(1), "10:00") not in out
    assert hm(out)[0] == "10:30"


async def test_buffer_from_db(world: World) -> None:
    async with write_tx(world.sm) as s:
        await barbers.update_barber(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, buffer_min=15
        )
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        out = hm(
            await slots.get_free_slots(
                s, barber_id=world.ali_id, service_id=world.svc30, day=D(1), now=NOW
            )
        )
    assert "10:30" not in out and "10:45" in out
