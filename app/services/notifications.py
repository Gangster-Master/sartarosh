"""Notification outbox: schedule / claim / mark (ARCHITECTURE.md §4.13).

Delivery is at most once: a claimed row is never handed out again [A4].
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ACTIVE_STATUSES, Awaiting, BookingSource, BookingStatus, NotificationKind
from app.db.models import Barber, Booking, Review, ScheduledNotification, User
from app.services import clock
from app.services._common import booking_dto, decision_targets, is_bookable, load_booking
from app.services.dto import DueNotification
from app.services.schedule import barber_windows
from app.services.settings import ShopSettings, get_shop_settings

K = NotificationKind

_STALE: dict[NotificationKind, timedelta] = {
    K.REMINDER_24H: timedelta(hours=3),
    K.REMINDER_2H: timedelta(minutes=30),
    K.RETURN: timedelta(days=3),
    K.REVIEW: timedelta(hours=24),
    K.MANUAL_VISIT_CONFIRM: timedelta(hours=24),
    K.COMPLETED_THANKS: timedelta(hours=24),
}


async def _upsert(
    session: AsyncSession, booking_id: int, kind: NotificationKind, send_at: datetime
) -> None:
    row = (
        await session.execute(
            select(ScheduledNotification).where(
                ScheduledNotification.booking_id == booking_id,
                ScheduledNotification.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(ScheduledNotification(booking_id=booking_id, kind=kind, send_at=send_at))
    elif row.claimed_at is None and row.sent_at is None:
        row.send_at = send_at
        row.cancelled_at = None
    await session.flush()


async def in_working_window(session: AsyncSession, barber_id: int, at: datetime) -> bool:
    """Internal: is ``at`` inside the barber's working window (break excluded)?"""
    day = clock.to_local(at).date()
    for w in await barber_windows(session, barber_id, day, day):
        if w.start <= at < w.end:
            return True
    return False


async def schedule_for_booking(
    session: AsyncSession, *, booking_id: int, now: datetime | None = None
) -> None:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    b = await load_booking(session, booking_id)
    s = await get_shop_settings(session)

    if b.status == BookingStatus.PENDING:
        if b.awaiting == Awaiting.BARBER and b.expire_at is not None:
            created = b.created_at or now
            send_at = created + timedelta(minutes=s.pending_reping_min)
            if await in_working_window(
                session, b.barber_id, created
            ) and send_at < b.expire_at - timedelta(minutes=5):
                await _upsert(session, b.id, K.PENDING_REPING, send_at)
        return

    if b.status == BookingStatus.CONFIRMED:
        await cancel_for_booking(session, booking_id=b.id, kinds=[K.PENDING_REPING], now=now)
        if b.source != BookingSource.BOT or b.client_id is None:
            return  # no client pushes for manual bookings
        if s.reminder_24h_enabled and b.start_at - now > timedelta(hours=24):
            await _upsert(session, b.id, K.REMINDER_24H, b.start_at - timedelta(hours=24))
        if s.reminder_2h_enabled and b.start_at - now > timedelta(hours=2):
            await _upsert(session, b.id, K.REMINDER_2H, b.start_at - timedelta(hours=2))
        return

    if b.status == BookingStatus.COMPLETED:
        await cancel_for_booking(
            session,
            booking_id=b.id,
            kinds=[K.PENDING_REPING, K.REMINDER_24H, K.REMINDER_2H],
            now=now,
        )
        if b.client_id is None:
            return
        if b.auto_completed and b.source == BookingSource.BOT:
            # auto-completion often runs at night: say thanks by day, not at 03:30
            await _upsert(session, b.id, K.COMPLETED_THANKS, clock.clamp_to_daytime(now))
        if b.source == BookingSource.MANUAL and b.visit_confirmed is not True:
            # walk-in [B9]: only the visit check until the client confirms the visit
            if b.visit_confirmed is None:
                # auto-completion runs at end + 12 h, often at night: never ask before 10:00
                await _upsert(session, b.id, K.MANUAL_VISIT_CONFIRM, clock.clamp_to_daytime(now))
            return
        if s.return_reminder_weeks > 0:
            await _upsert(
                session,
                b.id,
                K.RETURN,
                clock.clamp_to_daytime(b.start_at + timedelta(weeks=s.return_reminder_weeks)),
            )
        if s.reviews_enabled and not b.auto_completed:
            await _upsert(
                session,
                b.id,
                K.REVIEW,
                clock.clamp_to_daytime(b.end_at + timedelta(hours=s.review_delay_hours)),
            )
        return


async def schedule_decision_retry(
    session: AsyncSession, *, booking_id: int, now: datetime | None = None
) -> bool:
    """No decision message reached anyone (Telegram down): re-ping later, even outside
    working hours, so the request is not left to expire unseen. Returns True if queued."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    b = await load_booking(session, booking_id)
    if b.status != BookingStatus.PENDING or b.awaiting != Awaiting.BARBER or b.expire_at is None:
        return False
    s = await get_shop_settings(session)
    send_at = now + timedelta(minutes=max(1, s.pending_reping_min))
    if send_at >= b.expire_at - timedelta(minutes=5):
        return False
    await _upsert(session, b.id, K.PENDING_REPING, send_at)
    return True


async def cancel_for_booking(
    session: AsyncSession,
    *,
    booking_id: int,
    kinds: list[NotificationKind] | None = None,
    now: datetime | None = None,
) -> None:
    """Cancel unclaimed rows of the booking (all kinds by default)."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    stmt = select(ScheduledNotification).where(
        ScheduledNotification.booking_id == booking_id,
        ScheduledNotification.claimed_at.is_(None),
        ScheduledNotification.sent_at.is_(None),
        ScheduledNotification.cancelled_at.is_(None),
    )
    if kinds:
        stmt = stmt.where(ScheduledNotification.kind.in_(list(kinds)))
    for row in (await session.execute(stmt)).scalars():
        row.cancelled_at = now
    await session.flush()


async def _is_stale(row: ScheduledNotification, b: Booking, now: datetime) -> bool:
    if row.kind == K.PENDING_REPING:
        return b.expire_at is None or now > b.expire_at - timedelta(minutes=5)
    limit = _STALE.get(row.kind)
    return limit is not None and now > row.send_at + limit


async def _is_invalid(
    session: AsyncSession,
    row: ScheduledNotification,
    b: Booking,
    now: datetime,
    s: ShopSettings,
) -> bool:
    kind = row.kind
    if kind != K.PENDING_REPING:
        if b.client_id is None:
            return True
        client = await session.get(User, b.client_id)
        if client is None or client.deleted_at is not None or client.is_blocked:
            return True
    # the admin may have turned a push off after the row was queued
    if (
        (kind == K.REMINDER_24H and not s.reminder_24h_enabled)
        or (kind == K.REMINDER_2H and not s.reminder_2h_enabled)
        or (kind == K.REVIEW and not s.reviews_enabled)
        or (kind == K.RETURN and s.return_reminder_weeks <= 0)
    ):
        return True
    if (
        kind in (K.RETURN, K.REVIEW)
        and b.source == BookingSource.MANUAL
        and b.visit_confirmed is not True
    ):
        return True  # walk-in the client has not confirmed [B9]
    if kind in (K.REMINDER_24H, K.REMINDER_2H):
        if b.status != BookingStatus.CONFIRMED or b.start_at <= now:
            return True
        if kind == K.REMINDER_2H and b.client_ack_at is not None:
            return True
        return False
    if kind == K.PENDING_REPING:
        return not (b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER)
    if kind == K.RETURN:
        if b.status != BookingStatus.COMPLETED:
            return True
        barber = await session.get(Barber, b.barber_id)
        if barber is None or not await is_bookable(session, barber):
            return True  # "your barber is waiting" would be false
        newer = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(
                    Booking.client_id == b.client_id,
                    Booking.id != b.id,
                    ((Booking.status == BookingStatus.COMPLETED) & (Booking.start_at > b.start_at))
                    | Booking.status.in_(list(ACTIVE_STATUSES)),
                )
            )
        ).scalar_one()
        return newer > 0
    if kind == K.REVIEW:
        if b.status != BookingStatus.COMPLETED:
            return True
        return await session.get(Review, b.id) is not None
    if kind == K.MANUAL_VISIT_CONFIRM:
        return b.status != BookingStatus.COMPLETED or b.visit_confirmed is not None
    if kind == K.COMPLETED_THANKS:
        return b.status != BookingStatus.COMPLETED  # e.g. flipped to no-show meanwhile
    return False


async def claim_due(
    session: AsyncSession, *, limit: int = 50, now: datetime | None = None
) -> list[DueNotification]:
    """Claim due rows; cancel stale or invalid ones in the same transaction."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    out: list[DueNotification] = []
    s = await get_shop_settings(session)
    # Loop so a batch full of stale rows does not starve valid ones.
    for _ in range(10):
        rows = (
            (
                await session.execute(
                    select(ScheduledNotification)
                    .where(
                        ScheduledNotification.send_at <= now,
                        ScheduledNotification.claimed_at.is_(None),
                        ScheduledNotification.sent_at.is_(None),
                        ScheduledNotification.cancelled_at.is_(None),
                    )
                    .order_by(ScheduledNotification.send_at, ScheduledNotification.id)
                    .limit(limit - len(out))
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        for row in rows:
            b = await session.get(Booking, row.booking_id)
            if (
                b is None
                or await _is_stale(row, b, now)
                or await _is_invalid(session, row, b, now, s)
            ):
                row.cancelled_at = now
                continue
            row.claimed_at = now
            targets = await decision_targets(session, b) if row.kind == K.PENDING_REPING else []
            out.append(
                DueNotification(
                    id=row.id, kind=row.kind, booking=await booking_dto(session, b), targets=targets
                )
            )
        await session.flush()
        if len(out) >= limit:
            break
    return out


async def mark_sent(
    session: AsyncSession, *, notification_id: int, now: datetime | None = None
) -> None:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    row = await session.get(ScheduledNotification, notification_id)
    if row is None:
        return
    row.sent_at = now
    if row.claimed_at is None:
        row.claimed_at = now
    await session.flush()


async def mark_failed(session: AsyncSession, *, notification_id: int, error: str) -> None:
    """attempts += 1, last_error; the row stays claimed (at most once) [A4]."""
    row = await session.get(ScheduledNotification, notification_id)
    if row is None:
        return
    row.attempts = (row.attempts or 0) + 1
    row.last_error = (error or "")[:200]
    if row.claimed_at is None:
        row.claimed_at = clock.now_utc()
    await session.flush()


async def retry_later(session: AsyncSession, *, notification_id: int, seconds: float) -> None:
    """Telegram asked to wait (RetryAfter): unclaim the row and send it again later."""
    row = await session.get(ScheduledNotification, notification_id)
    if row is None or row.sent_at is not None:
        return
    row.attempts = (row.attempts or 0) + 1
    row.last_error = f"retry after {int(seconds)} s"
    row.claimed_at = None
    row.send_at = clock.now_utc() + timedelta(seconds=seconds)
    await session.flush()
