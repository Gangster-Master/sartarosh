"""Public barber rating: average + count on BarberDTO and in the bot UI."""

from __future__ import annotations

from datetime import timedelta

from app.bot.keyboards.client import barbers_kb, rating_badge
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, catalog, reviews
from tests.factories import D, L, World, book


async def _rated_visit(w: World, day: int, rating: int) -> None:
    c = await w.new_client()
    start = L(D(day), "10:00")
    r = await book(w, c, start, now=start - timedelta(hours=2))
    async with write_tx(w.sm) as s:
        await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=w.ali_user_id,
            now=start + timedelta(minutes=30),
        )
        await reviews.submit_rating(s, booking_id=r.booking.id, client_user_id=c, rating=rating)


async def test_no_ratings_yet(world: World) -> None:
    async with read_tx(world.sm) as s:
        b = await barbers.get_barber(s, world.ali_id)
    assert b.rating_avg is None and b.rating_count == 0
    assert rating_badge(b) == ""


async def test_average_and_count_visible_to_clients(world: World) -> None:
    for day, rating in enumerate([5, 4, 4]):
        await _rated_visit(world, day, rating)
    other_id, _ = await world.new_barber("Bekzod")
    async with read_tx(world.sm) as s:
        listed = {b.id: b for b in await catalog.list_barbers_for_service(s, service_id=None)}
        public = {b.id: b for b in await barbers.list_barbers(s, public_only=True)}
    for got in (listed, public):
        assert got[world.ali_id].rating_avg == 4.3 and got[world.ali_id].rating_count == 3
        assert got[other_id].rating_count == 0
    labels = [
        b.text
        for row in barbers_kb(list(listed.values()), "uz", back=True).inline_keyboard
        for b in row
    ]
    assert "💈 Ali ⭐4.3 (3)" in labels
    assert "💈 Bekzod" in labels
