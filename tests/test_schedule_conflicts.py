from __future__ import annotations

from datetime import time, timedelta

import pytest

from app.db.enums import BookingStatus, CancelledBy
from app.db.uow import read_tx, write_tx
from app.services import bookings, schedule, slots
from app.services.errors import InputError, PermissionDeniedError
from tests.factories import NOW, D, L, World, book


async def _block(w: World, actor: int, barber_id, s, e, cancel: bool):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as ses:
        return await schedule.add_time_off(
            ses,
            actor_user_id=actor,
            barber_id=barber_id,
            start_at=s,
            end_at=e,
            reason="dars",
            cancel_conflicts=cancel,
            now=NOW,
        )


async def test_preview_and_cancel_conflicts(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "15:30"))
    async with read_tx(world.sm) as s:
        prev = await schedule.preview_time_off_conflicts(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            start_at=L(D(1), "15:00"),
            end_at=L(D(1), "17:00"),
        )
    assert [b.id for b in prev] == [r.booking.id]
    res = await _block(
        world, world.ali_user_id, world.ali_id, L(D(1), "15:00"), L(D(1), "17:00"), True
    )
    assert [b.id for b in res.cancelled] == [r.booking.id] and res.overlapping_kept == []
    assert res.cancelled[0].status == BookingStatus.CANCELLED_BY_BARBER
    assert res.cancelled[0].cancelled_by == CancelledBy.BARBER
    async with read_tx(world.sm) as s:
        free = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=D(1), now=NOW
        )
    assert not any(L(D(1), "15:00") <= t < L(D(1), "17:00") for t in free)


async def test_keep_returns_overlapping(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "15:30"))
    res = await _block(
        world, world.ali_user_id, world.ali_id, L(D(1), "15:00"), L(D(1), "17:00"), False
    )
    assert res.cancelled == [] and [b.id for b in res.overlapping_kept] == [r.booking.id]
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.CONFIRMED


async def test_bounds_snapped_to_grid(world: World) -> None:
    res = await _block(
        world, world.ali_user_id, world.ali_id, L(D(1), "15:07"), L(D(1), "16:52"), False
    )
    assert res.time_off.start_at == L(D(1), "15:00") and res.time_off.end_at == L(D(1), "17:00")


async def test_hours_shrink_lists_out_of_hours(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "19:00"))
    async with write_tx(world.sm) as s:
        res = await schedule.set_working_hours(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            weekday=D(1).weekday(),
            start_time=time(10),
            end_time=time(18),
            now=NOW,
        )
    assert [b.id for b in res.out_of_hours] == [r.booking.id]
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.CONFIRMED


async def test_hours_validation(world: World) -> None:
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await schedule.set_working_hours(
                s,
                actor_user_id=world.admin_id,
                barber_id=world.ali_id,
                weekday=0,
                start_time=time(10, 10),
                end_time=time(20),
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await schedule.set_working_hours(
                s,
                actor_user_id=world.admin_id,
                barber_id=world.ali_id,
                weekday=0,
                start_time=time(20),
                end_time=time(10),
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await schedule.set_working_hours(
                s,
                actor_user_id=world.ali_user_id,
                barber_id=world.ali_id,
                weekday=0,
                start_time=time(10),
                end_time=time(20),
            )


async def test_copy_to_all(world: World) -> None:
    async with write_tx(world.sm) as s:
        await schedule.set_working_hours(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            weekday=0,
            start_time=time(9),
            end_time=time(18),
            now=NOW,
        )
        await schedule.copy_hours_to_all_weekdays(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, source_weekday=0, now=NOW
        )
    async with read_tx(world.sm) as s:
        week = await schedule.get_week_hours(s, world.ali_id)
        shop = await schedule.get_shop_week_hours(s)
    assert [w.weekday for w in week] == [0, 1, 2, 3, 4, 5]
    assert all(w.start_time == time(9) and w.break_start is None for w in week)
    assert shop[6].open_time is None and shop[0].open_time == time(9)


async def test_busy_now(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "14:30"), now=L(D(0), "13:30"))
    async with write_tx(world.sm) as s:
        res = await schedule.busy_now(
            s, actor_user_id=world.ali_user_id, minutes=30, now=L(D(0), "14:07")
        )
    assert res.time_off.start_at == L(D(0), "14:00") and res.time_off.end_at == L(D(0), "14:45")
    assert res.cancelled == [] and [b.id for b in res.overlapping_kept] == [r.booking.id]
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.CONFIRMED
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await schedule.busy_now(s, actor_user_id=world.ali_user_id, minutes=45, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await schedule.busy_now(s, actor_user_id=c, minutes=30, now=NOW)


async def test_barber_block_refused_by_setting(world: World) -> None:
    await world.set("barbers_can_block_time", "false")
    with pytest.raises(PermissionDeniedError):
        await _block(
            world, world.ali_user_id, world.ali_id, L(D(1), "15:00"), L(D(1), "16:00"), False
        )
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await schedule.busy_now(s, actor_user_id=world.ali_user_id, minutes=30, now=NOW)
    # admin still can
    await _block(world, world.admin_id, world.ali_id, L(D(1), "15:00"), L(D(1), "16:00"), False)


async def test_shop_wide_admin_only(world: World) -> None:
    with pytest.raises(PermissionDeniedError):
        await _block(world, world.ali_user_id, None, L(D(2), "00:00"), L(D(3), "00:00"), False)
    bek, bek_user = await world.new_barber("Bekzod")
    with pytest.raises(PermissionDeniedError):
        await _block(world, bek_user, world.ali_id, L(D(1), "15:00"), L(D(1), "16:00"), False)
    c1, c2 = await world.new_client(), await world.new_client()
    r1 = await book(world, c1, L(D(2), "10:00"))
    r2 = await book(world, c2, L(D(2), "10:00"), barber_id=bek)
    iv = schedule.full_day_interval(D(2))
    res = await _block(world, world.admin_id, None, iv.start, iv.end, True)
    assert {b.id for b in res.cancelled} == {r1.booking.id, r2.booking.id}
    assert all(b.cancelled_by == CancelledBy.ADMIN for b in res.cancelled)
    async with read_tx(world.sm) as s:
        days = await slots.get_available_days(s, barber_id=None, service_id=world.svc30, now=NOW)
    assert days[2].is_working is False


async def test_delete_time_off(world: World) -> None:
    res = await _block(
        world, world.ali_user_id, world.ali_id, L(D(1), "15:00"), L(D(1), "16:00"), False
    )
    async with write_tx(world.sm) as s:
        await schedule.delete_time_off(
            s, actor_user_id=world.ali_user_id, time_off_id=res.time_off.id
        )
    async with read_tx(world.sm) as s:
        assert (
            await schedule.list_time_off(
                s, barber_id=world.ali_id, start=L(D(1), "00:00"), end=L(D(2), "00:00")
            )
            == []
        )
    assert timedelta
