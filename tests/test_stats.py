from __future__ import annotations

from datetime import timedelta

from app.db.uow import read_tx, write_tx
from app.services import bookings, stats
from tests.factories import D, L, World, book


async def _manual(w: World, start, phone=None):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await bookings.create_manual_booking(
            s,
            actor_user_id=w.admin_id,
            barber_id=w.ali_id,
            service_id=w.svc30,
            start_at=start,
            client_name="Walk In",
            client_phone=phone,
            now=start - timedelta(hours=1),
        )


async def _done(w: World, bid: int, at) -> None:  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        await bookings.complete_booking(s, booking_id=bid, actor_user_id=w.admin_id, now=at)


async def test_local_day_bucketing_and_revenue(world: World) -> None:
    # Ali works 10-20; make the test day a long one to place bookings at 23:30 and 00:30
    from datetime import time

    from app.services import schedule

    async with write_tx(world.sm) as s:
        for wd in range(7):
            await schedule.set_working_hours(
                s,
                actor_user_id=world.admin_id,
                barber_id=world.ali_id,
                weekday=wd,
                start_time=time(0),
                end_time=time(23, 45),
            )
    late = await _manual(world, L(D(1), "23:00"))  # Tue 23:00 local = Tue 18:00 UTC
    early = await _manual(world, L(D(2), "00:30"))  # Wed 00:30 local = Tue 19:30 UTC!
    await _done(world, late.id, L(D(1), "23:30"))
    await _done(world, early.id, L(D(2), "01:00"))
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.cancel_by_client(
            s, booking_id=r.booking.id, client_user_id=c, now=L(D(0), "09:00")
        )
    async with read_tx(world.sm) as s:
        tue = await stats.get_stats(s, actor_user_id=world.admin_id, start=D(1), end=D(1))
        wed = await stats.get_stats(s, actor_user_id=world.admin_id, start=D(2), end=D(2))
        both = await stats.get_stats(s, actor_user_id=world.admin_id, start=D(1), end=D(2))
    assert tue.totals.total == 2 and tue.totals.completed == 1 and tue.totals.cancelled == 1
    assert wed.totals.total == 1 and wed.totals.completed == 1
    assert both.totals.revenue_uzs == 120_000
    assert both.totals.manual_completed == 2
    assert both.top_services[0].service_name == "Soch olish" and both.top_services[0].completed == 2
    assert [row.barber_name for row in both.rows] == ["Ali"]


async def test_revenue_uses_final_price(world: World) -> None:
    await world.set("welcome_bonus", "5")
    c = await world.new_client()
    start = L(D(0), "10:00")
    r = await book(world, c, start)
    await _done(world, r.booking.id, start)
    r2 = await book(world, c, L(D(1), "10:00"), use_bonus=True)
    await _done(world, r2.booking.id, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        st = await stats.get_stats(s, actor_user_id=world.admin_id, start=D(0), end=D(1))
    assert st.totals.completed == 2 and st.totals.revenue_uzs == 60_000
