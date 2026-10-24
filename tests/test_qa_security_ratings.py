"""QA regression (security_abuse): rating integrity now that ratings are public."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.bot.callbacks import RateCb
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, reviews, users
from app.services.errors import ServiceError
from tests.bot_harness import Harness
from tests.factories import ENV_ADMIN_TG, D, L, World, book


async def _rating(w: World):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        b = await barbers.get_barber(s, w.ali_id)
    return b.rating_avg, b.rating_count


async def test_barber_cannot_rate_his_own_visit(world: World) -> None:
    """A barber books himself (bot flow), marks it done and rates himself 5 stars."""
    async with write_tx(world.sm) as s:
        await users.set_verified_contact(s, user_id=world.ali_user_id, raw_phone="+998901112233")
    start = L(D(0), "10:00")
    r = await book(world, world.ali_user_id, start, now=start - timedelta(hours=2))
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=world.ali_user_id,
            now=start + timedelta(minutes=30),
        )
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await reviews.submit_rating(
                s, booking_id=r.booking.id, client_user_id=world.ali_user_id, rating=5
            )
    assert await _rating(world) == (None, 0)


async def test_barber_cannot_self_rate_via_walkin_with_own_phone(world: World) -> None:
    async with write_tx(world.sm) as s:
        await users.set_verified_contact(s, user_id=world.ali_user_id, raw_phone="+998901112233")
    start = L(D(0), "10:00")
    async with write_tx(world.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=start,
            client_name="Me",
            client_phone="+998901112233",
            now=start - timedelta(minutes=5),
        )
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=world.ali_user_id, now=start + timedelta(minutes=30)
        )
    try:  # refused too once the walk-in is not linked to the barber himself
        async with write_tx(world.sm) as s:
            await bookings.confirm_manual_visit(
                s,
                booking_id=b.id,
                client_user_id=world.ali_user_id,
                yes=True,
                now=start + timedelta(minutes=31),
            )
    except ServiceError:
        pass
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await reviews.submit_rating(
                s, booking_id=b.id, client_user_id=world.ali_user_id, rating=5
            )
    assert await _rating(world) == (None, 0)


async def test_denied_walkin_cannot_be_rated(world: World) -> None:
    """D-2: the client said the walk-in never happened, yet can rate it publicly."""
    c = await world.new_client(phone="+998901234567")
    start = L(D(0), "10:00")
    async with write_tx(world.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=start,
            client_name="Walk",
            client_phone="+998901234567",
            now=start - timedelta(minutes=5),
        )
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=world.ali_user_id, now=start + timedelta(minutes=30)
        )
        await bookings.confirm_manual_visit(
            s, booking_id=b.id, client_user_id=c, yes=False, now=start + timedelta(minutes=31)
        )
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await reviews.submit_rating(s, booking_id=b.id, client_user_id=c, rating=1)
    assert await _rating(world) == (None, 0)


async def test_rating_dropped_after_no_show_flip(world: World) -> None:
    """D-3: an auto-completed visit is rated via a crafted button, then flipped to no-show."""
    c = await world.new_client()
    start = L(D(0), "10:00")
    r = await book(world, c, start, now=start - timedelta(hours=2))
    async with write_tx(world.sm) as s:
        done = await bookings.auto_complete_due(s, now=start + timedelta(hours=14))
    assert any(x.booking.id == r.booking.id for x in done)
    rated = True
    try:
        async with write_tx(world.sm) as s:
            await reviews.submit_rating(s, booking_id=r.booking.id, client_user_id=c, rating=5)
    except ServiceError:
        rated = False  # also acceptable: auto-completed visits are not ratable
    async with write_tx(world.sm) as s:
        await bookings.mark_no_show(
            s,
            booking_id=r.booking.id,
            actor_user_id=world.ali_user_id,
            now=start + timedelta(hours=14),
        )
    assert await _rating(world) == (None, 0), f"rated={rated}: no-show still counts publicly"


# ------------------------------------------------------------------ bot: alert spam


@pytest.fixture
def h(world: World) -> Harness:
    return Harness(world.sm)


async def test_low_rating_alert_sent_once_on_repeat_taps(world: World, h: Harness) -> None:
    """A client re-taps the 1-star button: every tap pages all admins again."""
    c = await world.new_client()
    async with read_tx(world.sm) as s:
        tg = (await users.get_user(s, c)).tg_id
    start = L(D(0), "10:00")
    r = await book(world, c, start, now=start - timedelta(hours=2))
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=world.ali_user_id,
            now=start + timedelta(minutes=30),
        )
    data = RateCb(id=r.booking.id, v=1).pack()
    for _ in range(5):
        await h.tap(tg, data, message_id=777)  # not "fast": > 1 s apart, debounce passes
    admin_msgs = h.texts_to(ENV_ADMIN_TG)
    assert len(admin_msgs) == 1, f"admin got {len(admin_msgs)} low-rating alerts"
