"""Ratings 1..5 [P2] (ARCHITECTURE.md §4.14)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BookingSource, BookingStatus
from app.db.models import Barber, Review
from app.services import clock
from app.services._common import load_booking
from app.services.dto import ReviewResult
from app.services.errors import InputError, InvalidTransitionError, PermissionDeniedError
from app.services.settings import get_shop_settings

LOW_RATING_MAX = 3


async def submit_rating(
    session: AsyncSession, *, booking_id: int, client_user_id: int, rating: int
) -> ReviewResult:
    b = await load_booking(session, booking_id)
    if b.client_id is None or b.client_id != client_user_id:
        raise PermissionDeniedError("not your booking")
    if b.status != BookingStatus.COMPLETED:
        raise InvalidTransitionError("not completed", current=b.status.value)
    existing = await session.get(Review, booking_id)
    if existing is not None:  # idempotent
        return ReviewResult(
            booking_id=booking_id,
            rating=existing.rating,
            is_low=existing.rating <= LOW_RATING_MAX,
            created=False,
        )
    # Ratings are public: only visits that surely happened, by real clients, count.
    if b.auto_completed:  # nobody confirmed the visit; it may still flip to no-show [D-3]
        raise InvalidTransitionError("auto-completed visit", current=b.status.value)
    if b.source == BookingSource.MANUAL and b.visit_confirmed is not True:  # [D-2]
        raise InvalidTransitionError("walk-in not confirmed by the client", current=b.status.value)
    barber = await session.get(Barber, b.barber_id)
    if barber is not None and barber.user_id == client_user_id:
        raise PermissionDeniedError("a barber can't rate himself")
    if not (await get_shop_settings(session)).reviews_enabled:
        raise InvalidTransitionError("reviews disabled", current=b.status.value)
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise InputError("rating 1..5", field="rating") from None
    if not 1 <= rating <= 5:
        raise InputError("rating 1..5", field="rating")
    session.add(
        Review(
            booking_id=booking_id,
            user_id=client_user_id,
            barber_id=b.barber_id,
            rating=rating,
            created_at=clock.now_utc(),
        )
    )
    await session.flush()
    return ReviewResult(booking_id=booking_id, rating=rating, is_low=rating <= LOW_RATING_MAX)
