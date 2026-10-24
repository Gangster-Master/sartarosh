"""Bookings: create, state machine, proposals, queries (ARCHITECTURE.md §4.11).

Invariant: a booking in ACTIVE_STATUSES owns exactly one ``booking_slots`` row per
15-minute cell of ``[start_at, block_end_at)``; any other status owns none. All
cell changes go through ``_occupy`` / ``_release``.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONSENT_VERSION
from app.db.enums import (
    ACTIVE_STATUSES,
    Awaiting,
    BookingSource,
    BookingStatus,
    CancelledBy,
    NotificationKind,
)
from app.db.models import Barber, Booking, BookingMessage, BookingSlot, Review, Service, User
from app.services import bonus as _bonus
from app.services import clock, notifications
from app.services._common import (
    add_user_counter,
    booking_dto,
    booking_dtos,
    decision_targets,
    is_bookable,
    load_barber,
    load_booking,
    load_user,
    phone_mask_cutoff,
    require_admin,
    require_barber_or_admin,
    roles_for,
)
from app.services.audit import write_audit
from app.services.catalog import (
    _barbers_for_service_rows,
    list_barber_services,
    resolve_service,
    shop_has_services,
)
from app.services.dto import (
    BookingCreated,
    BookingDTO,
    CancelResult,
    ClientBookings,
    CompleteResult,
    NoShowResult,
    Page,
    PendingDigestItem,
    ProposalResult,
    RebookHint,
)
from app.services.errors import (
    BarberInactiveError,
    ClientLimitError,
    ConsentRequiredError,
    InputError,
    InsufficientBonusError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    PhoneRequiredError,
    ProposalLimitError,
    SlotTakenError,
    SlotUnavailableError,
    TooEarlyError,
    TooLateError,
    UserBlockedError,
)
from app.services.schedule import barber_windows
from app.services.settings import ShopSettings, get_shop_settings
from app.services.slots import active_count_on_day, is_offered_start, slot_problem
from app.services.users import get_client_stats, is_soft_blocked
from app.services.validation import normalize_phone, validate_comment, validate_name

__all__ = [
    "CancelledBy",
    "add_decision_message",
    "answer_proposal",
    "auto_complete_due",
    "bookings_for_daily_summary",
    "cancel_by_client",
    "cancel_by_staff",
    "client_ack",
    "complete_booking",
    "compute_expire_at",
    "confirm_booking",
    "confirm_manual_visit",
    "create_booking",
    "create_manual_booking",
    "expire_due_pending",
    "get_booking",
    "get_booking_for_actor",
    "get_last_completed",
    "list_barber_day",
    "list_bookings",
    "list_client_bookings",
    "list_decision_messages",
    "list_pending_digest",
    "list_unresolved",
    "mark_no_show",
    "propose_time",
    "reject_booking",
]

_ACTIVE = list(ACTIVE_STATUSES)
_UNIT = timedelta(minutes=clock.SLOT_UNIT_MIN)
NK = NotificationKind


def _now(now: datetime | None) -> datetime:
    return clock.ensure_aware(now) if now is not None else clock.now_utc()


def _invalid(b: Booking) -> InvalidTransitionError:
    return InvalidTransitionError("invalid transition", current=b.status.value)


# ------------------------------------------------------------------ cells


def _cells(b: Booking) -> list[datetime]:
    out = []
    t = clock.floor_to_grid(b.start_at)
    while t < b.block_end_at:
        out.append(t)
        t += _UNIT
    return out


async def _occupy(session: AsyncSession, booking: Booking) -> None:
    """Insert the booking's cells inside a SAVEPOINT; a PK conflict -> SlotTakenError."""
    rows = [
        {"barber_id": booking.barber_id, "slot_start": t, "booking_id": booking.id}
        for t in _cells(booking)
    ]
    try:
        async with session.begin_nested():
            await session.execute(insert(BookingSlot), rows)
    except IntegrityError:
        raise SlotTakenError("slot taken") from None


async def _release(session: AsyncSession, booking_id: int) -> None:
    await session.execute(delete(BookingSlot).where(BookingSlot.booking_id == booking_id))


# ------------------------------------------------------------------ internal transitions


async def _active_bookings_of_client(session: AsyncSession, user_id: int) -> list[Booking]:
    return list(
        (
            await session.execute(
                select(Booking)
                .where(Booking.client_id == user_id, Booking.status.in_(_ACTIVE))
                .order_by(Booking.start_at)
            )
        ).scalars()
    )


async def _cancel(
    session: AsyncSession,
    b: Booking,
    *,
    status: BookingStatus,
    by: CancelledBy,
    reason: str | None,
    now: datetime,
) -> None:
    """Internal: move an ACTIVE booking to a cancelled/terminal status and free its cells."""
    b.status = status
    b.cancelled_by = by
    b.cancel_reason = (reason or "")[:200] or None
    b.decided_at = b.decided_at or now
    await session.flush()
    await _release(session, b.id)
    await notifications.cancel_for_booking(session, booking_id=b.id, now=now)


async def _barber_actor(session: AsyncSession, actor_user_id: int, b: Booking) -> bool:
    """Actor must be the booking's barber or an admin. Returns is_admin."""
    _, is_admin = await require_barber_or_admin(session, actor_user_id, b.barber_id)
    return is_admin


def _client_actor(b: Booking, client_user_id: int) -> None:
    if b.client_id is None or b.client_id != client_user_id:
        raise PermissionDeniedError("not your booking")


async def _staff_by(session: AsyncSession, actor_user_id: int, b: Booking) -> CancelledBy:
    barber = await session.get(Barber, b.barber_id)
    if barber is not None and barber.user_id == actor_user_id:
        return CancelledBy.BARBER
    return CancelledBy.ADMIN


async def _dto(session: AsyncSession, b: Booking) -> BookingDTO:
    return await booking_dto(session, b)


async def _client_balance(session: AsyncSession, client_id: int | None) -> int:
    if client_id is None:
        return 0
    return (
        await session.execute(select(User.bonus_balance).where(User.id == client_id))
    ).scalar_one_or_none() or 0


async def _is_postgres(session: AsyncSession) -> bool:
    return (await session.connection()).dialect.name == "postgresql"


async def _load_locked(session: AsyncSession, booking_id: int) -> Booking:
    """load_booking for a state transition: row lock on PostgreSQL so two concurrent
    decisions can't both apply (SQLite is already serialised by BEGIN IMMEDIATE)."""
    if await _is_postgres(session):
        b = await session.get(Booking, booking_id, with_for_update=True)
        if b is None:
            raise NotFoundError("booking not found", entity="booking", id=booking_id)
        return b
    return await load_booking(session, booking_id)


# ------------------------------------------------------------------ expiry


async def compute_expire_at(
    session: AsyncSession, *, barber_id: int, created_at: datetime, start_at: datetime
) -> datetime:
    """Pending decision deadline [B1][B2] (see ARCHITECTURE.md §4.11)."""
    created_at = clock.ensure_aware(created_at)
    start_at = clock.ensure_aware(start_at)
    s = await get_shop_settings(session)
    cutoff = start_at - timedelta(minutes=s.pending_cutoff_before_start_min)
    first_day = clock.to_local(created_at).date()
    last_day = max(first_day, clock.to_local(start_at).date())
    windows = sorted(
        await barber_windows(session, barber_id, first_day, last_day), key=lambda w: w.start
    )
    base: datetime | None = None
    for w in windows:
        if w.start <= created_at < w.end:
            base = created_at
            break
    if base is None:
        for w in windows:
            if created_at < w.start < start_at:
                base = w.start
                break
    if base is None or base >= cutoff:
        expire = cutoff
    else:
        expire = min(base + timedelta(minutes=s.pending_timeout_min), cutoff)
    expire = max(expire, created_at + timedelta(minutes=s.min_decision_min))
    return min(expire, start_at)


# ------------------------------------------------------------------ create


async def create_booking(
    session: AsyncSession,
    *,
    client_user_id: int,
    barber_id: int | None,
    service_id: int | None,
    start_at: datetime,
    comment: str | None = None,
    use_bonus: bool = False,
    now: datetime | None = None,
) -> BookingCreated:
    now = _now(now)
    # 1. time + comment
    try:
        start_at = clock.ensure_aware(start_at)
    except ValueError:
        raise SlotUnavailableError("naive start") from None
    if not clock.is_on_grid(start_at):
        raise SlotUnavailableError("start not on grid")
    comment = validate_comment(comment) if comment is not None else None

    # 2. user checks
    user = await load_user(session, client_user_id, fresh=True)
    if user.deleted_at is not None:
        raise NotFoundError("user deleted", entity="user", id=client_user_id)
    if user.is_blocked:
        raise UserBlockedError("user blocked")
    if (user.consent_version or 0) < CONSENT_VERSION:
        raise ConsentRequiredError("consent required")
    if not user.phone or not user.phone_verified:
        raise PhoneRequiredError("verified phone required")
    client_name = validate_name(user.full_name or user.first_name or "")
    phone = user.phone

    # 3. lock the client row (PG); SQLite is already serialised by BEGIN IMMEDIATE [A3]
    if await _is_postgres(session):
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())

    # 4. client limits, per user and per phone [D1]
    s = await get_shop_settings(session)
    client_cond = or_(Booking.client_id == user.id, Booking.client_phone == phone)
    active = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(client_cond, Booking.status.in_(_ACTIVE), Booking.end_at > now)
        )
    ).scalar_one()
    if active >= s.max_active_bookings:
        raise ClientLimitError("too many active", kind="max_active", limit=s.max_active_bookings)
    ds, de = clock.day_bounds_utc(clock.local_today(now))
    today = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                client_cond,
                Booking.source == BookingSource.BOT,
                Booking.created_at >= ds,
                Booking.created_at < de,
            )
        )
    ).scalar_one()
    if today >= s.max_requests_per_day:
        raise ClientLimitError(
            "too many requests today", kind="max_per_day", limit=s.max_requests_per_day
        )

    # 5. candidates
    if barber_id is not None:
        barber = await session.get(Barber, barber_id)
        if barber is None or not await is_bookable(session, barber):
            raise BarberInactiveError("barber not bookable")
    # the service step is skipped only when nothing is on offer (ARCHITECTURE Q2)
    if (
        service_id is None
        and await shop_has_services(session)
        and (barber_id is None or await list_barber_services(session, barber_id))
    ):
        raise InputError("service required", field="service")
    if barber_id is not None:
        candidates = [barber]
    else:
        day = clock.to_local(start_at).date()
        ranked = []
        for b in await _barbers_for_service_rows(session, service_id):
            try:
                rs = await resolve_service(session, barber_id=b.id, service_id=service_id)
            except InputError:
                continue
            prob = await slot_problem(
                session,
                barber_id=b.id,
                start_at=start_at,
                duration_min=rs.duration_min,
                buffer_min=b.buffer_min,
                now=now,
            )
            if prob is None and await is_offered_start(session, b, start_at):
                load = await active_count_on_day(session, b.id, day)
                ranked.append((load, b.sort_order or 0, b.id, b))
        if not ranked:
            raise SlotTakenError("no free barber")
        ranked.sort(key=lambda x: x[:3])
        candidates = [x[3] for x in ranked]

    soft = await is_soft_blocked(session, user.id, now=now)
    is_new = (await get_client_stats(session, user.id)).is_new

    # 6. try each candidate in a SAVEPOINT
    booking: Booking | None = None
    last_err: SlotTakenError | None = None
    for barber in candidates:
        try:
            async with session.begin_nested():
                rs = await resolve_service(session, barber_id=barber.id, service_id=service_id)
                end_at = start_at + timedelta(minutes=rs.duration_min)
                block_end_at = end_at + timedelta(minutes=barber.buffer_min)
                overlap = (
                    await session.execute(
                        select(func.count())
                        .select_from(Booking)
                        .where(
                            client_cond,
                            Booking.status.in_(_ACTIVE),
                            Booking.start_at < end_at,
                            Booking.end_at > start_at,
                        )
                    )
                ).scalar_one()
                if overlap:
                    raise ClientLimitError("overlapping booking", kind="overlap", limit=0)
                prob = await slot_problem(
                    session,
                    barber_id=barber.id,
                    start_at=start_at,
                    duration_min=rs.duration_min,
                    buffer_min=barber.buffer_min,
                    now=now,
                )
                if prob == "taken":
                    raise SlotTakenError("slot taken")
                if prob is not None:
                    raise SlotUnavailableError("slot unavailable")
                if barber_id is not None and not await is_offered_start(session, barber, start_at):
                    raise SlotUnavailableError("start not on the barber's slot grid")
                bonus_cost, final_price = 0, rs.price_uzs
                if use_bonus:
                    q = await _bonus.quote_redeem(session, user_id=user.id, price_uzs=rs.price_uzs)
                    if not q.possible:
                        raise InsufficientBonusError("not enough bonus")
                    bonus_cost, final_price = q.cost, q.final_price_uzs
                confirmed = (
                    barber.auto_accept
                    and not soft
                    and not (is_new and s.new_client_requires_approval)
                    and barber.is_reachable
                )
                b = Booking(
                    client_id=user.id,
                    barber_id=barber.id,
                    service_id=rs.service_id,
                    service_name_snapshot=rs.name,
                    start_at=start_at,
                    end_at=end_at,
                    block_end_at=block_end_at,
                    duration_min=rs.duration_min,
                    price_snapshot=rs.price_uzs,
                    bonus_cost=bonus_cost,
                    final_price_uzs=final_price,
                    status=BookingStatus.CONFIRMED if confirmed else BookingStatus.PENDING,
                    awaiting=None if confirmed else Awaiting.BARBER,
                    source=BookingSource.BOT,
                    client_name=client_name,
                    client_phone=phone,
                    client_phone_verified=True,
                    comment=comment,
                    decided_at=now if confirmed else None,
                    created_at=now,
                    updated_at=now,
                )
                if not confirmed:
                    b.expire_at = await compute_expire_at(
                        session, barber_id=barber.id, created_at=now, start_at=start_at
                    )
                session.add(b)
                await session.flush()
                await _occupy(session, b)
            booking = b
            break
        except SlotTakenError as e:
            last_err = e
            continue
    if booking is None:
        raise last_err or SlotTakenError("slot taken")

    # 7. outbox
    await notifications.schedule_for_booking(session, booking_id=booking.id, now=now)

    # 8. decision targets
    targets = []
    if booking.status == BookingStatus.PENDING:
        barber = await load_barber(session, booking.barber_id)
        targets = await decision_targets(session, booking, needed_approval=bool(barber.auto_accept))
    return BookingCreated(
        booking=await _dto(session, booking),
        auto_confirmed=booking.status == BookingStatus.CONFIRMED,
        client_soft_blocked=soft,
        decision_targets=targets,
    )


async def create_manual_booking(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    service_id: int | None,
    start_at: datetime,
    client_name: str,
    client_phone: str | None,
    comment: str | None = None,
    now: datetime | None = None,
) -> BookingDTO:
    """Walk-in / phone booking by the barber or an admin [#26]. CONFIRMED at once."""
    now = _now(now)
    await require_barber_or_admin(session, actor_user_id, barber_id)
    barber = await load_barber(session, barber_id)
    if not barber.is_active:
        raise BarberInactiveError("barber inactive")
    try:
        start_at = clock.ensure_aware(start_at)
    except ValueError:
        raise SlotUnavailableError("naive start") from None
    if not clock.is_on_grid(start_at):
        raise SlotUnavailableError("start not on grid")
    if start_at < clock.floor_to_grid(now) - _UNIT:
        raise SlotUnavailableError("start in the past")
    name = validate_name(client_name)
    phone = normalize_phone(client_phone) if client_phone and client_phone.strip() else None
    comment = validate_comment(comment) if comment is not None else None
    rs = await resolve_service(session, barber_id=barber_id, service_id=service_id)
    end_at = start_at + timedelta(minutes=rs.duration_min)
    prob = await slot_problem(
        session,
        barber_id=barber_id,
        start_at=start_at,
        duration_min=rs.duration_min,
        buffer_min=barber.buffer_min,
        now=now,
        ignore_lead_time=True,
    )
    if prob == "taken":
        raise SlotTakenError("slot taken")
    if prob is not None:
        raise SlotUnavailableError("slot unavailable")

    client_id: int | None = None
    if phone:
        # never link a walk-in to its own barber or to the staff member typing it [S-01]
        staff_ids = {actor_user_id} | ({barber.user_id} if barber.user_id is not None else set())
        matches = (
            (
                await session.execute(
                    select(User.id).where(
                        User.phone == phone,
                        User.phone_verified.is_(True),
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(matches) == 1 and matches[0] not in staff_ids:
            client_id = matches[0]

    b = Booking(
        client_id=client_id,
        barber_id=barber_id,
        service_id=rs.service_id,
        service_name_snapshot=rs.name,
        start_at=start_at,
        end_at=end_at,
        block_end_at=end_at + timedelta(minutes=barber.buffer_min),
        duration_min=rs.duration_min,
        price_snapshot=rs.price_uzs,
        bonus_cost=0,
        final_price_uzs=rs.price_uzs,
        status=BookingStatus.CONFIRMED,
        awaiting=None,
        source=BookingSource.MANUAL,
        client_name=name,
        client_phone=phone,
        client_phone_verified=False,
        comment=comment,
        decided_at=now,
        created_by_user_id=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(b)
    await session.flush()
    await _occupy(session, b)
    await notifications.schedule_for_booking(session, booking_id=b.id, now=now)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="booking.manual_create",
        entity="booking",
        entity_id=b.id,
        payload={"barber_id": barber_id, "start_at": start_at, "client_id": client_id},
    )
    return await _dto(session, b)


# ------------------------------------------------------------------ decision messages


async def add_decision_message(
    session: AsyncSession, *, booking_id: int, chat_id: int, message_id: int, role: str
) -> None:
    """Internal (notifier)."""
    try:
        async with session.begin_nested():
            session.add(
                BookingMessage(
                    booking_id=booking_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    role=(role or "")[:8],
                    created_at=clock.now_utc(),
                )
            )
            await session.flush()
    except IntegrityError:
        pass  # already stored


async def list_decision_messages(
    session: AsyncSession, booking_id: int
) -> list[tuple[int, int, str]]:
    rows = (
        await session.execute(
            select(BookingMessage.chat_id, BookingMessage.message_id, BookingMessage.role)
            .where(BookingMessage.booking_id == booking_id)
            .order_by(BookingMessage.id)
        )
    ).all()
    return [(c, m, r) for c, m, r in rows]


# ------------------------------------------------------------------ barber decisions


def _client_others(b: Booking):  # type: ignore[no-untyped-def]
    """COUNT of the client's other ACTIVE bookings (by user or phone), or None."""
    conds = []
    if b.client_id is not None:
        conds.append(Booking.client_id == b.client_id)
    if b.client_phone:
        conds.append(Booking.client_phone == b.client_phone)
    if not conds:
        return None
    return (
        select(func.count())
        .select_from(Booking)
        .where(or_(*conds), Booking.id != b.id, Booking.status.in_(_ACTIVE))
    )


async def _client_overlaps(
    session: AsyncSession, b: Booking, start_at: datetime, end_at: datetime
) -> bool:
    """Would ``b`` at [start_at, end_at) overlap another ACTIVE booking of its client?"""
    base = _client_others(b)
    if base is None:
        return False
    stmt = base.where(Booking.start_at < end_at, Booking.end_at > start_at)
    return bool((await session.execute(stmt)).scalar_one())


async def _late_accept_client_ok(
    session: AsyncSession, b: Booking, s: ShopSettings, now: datetime
) -> None:
    """A late accept must not give the client an overlapping booking or exceed the
    active-booking limit (the client may have rebooked elsewhere meanwhile), and the
    client must still be allowed to book."""
    if b.client_id is not None:
        client = await load_user(session, b.client_id, fresh=True)
        if client.deleted_at is not None or client.is_blocked:
            raise TooLateError("client can no longer book")
    base = _client_others(b)
    if base is None:
        return
    if await _client_overlaps(session, b, b.start_at, b.end_at):
        raise TooLateError("client already has another booking at this time")
    if b.source == BookingSource.BOT:
        active = (await session.execute(base.where(Booking.end_at > now))).scalar_one()
        if active >= s.max_active_bookings:
            raise TooLateError("client reached active booking limit")


async def confirm_booking(
    session: AsyncSession, *, booking_id: int, actor_user_id: int, now: datetime | None = None
) -> BookingDTO:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    if b.status == BookingStatus.CONFIRMED:
        return await _dto(session, b)
    s = await get_shop_settings(session)
    if b.status == BookingStatus.PENDING:
        if b.awaiting != Awaiting.BARBER:
            raise _invalid(b)
        if b.start_at <= now:
            raise TooLateError("booking already started")
    elif b.status == BookingStatus.EXPIRED:
        if b.awaiting != Awaiting.BARBER or b.expire_at is None:
            raise _invalid(b)
        if now > b.expire_at + timedelta(minutes=s.late_accept_grace_min) or b.start_at <= now:
            raise TooLateError("grace period passed")
        barber = await load_barber(session, b.barber_id)
        if not await is_bookable(session, barber):
            raise BarberInactiveError("barber not bookable")
        await _late_accept_client_ok(session, b, s, now)
        # the slot may have closed meanwhile (time off, new hours) [B5]
        prob = await slot_problem(
            session,
            barber_id=b.barber_id,
            start_at=b.start_at,
            duration_min=b.duration_min,
            buffer_min=barber.buffer_min,
            now=now,
            ignore_lead_time=True,
            ignore_booking_id=b.id,
            check_horizon=False,
        )
        if prob == "taken":
            raise SlotTakenError("slot taken")
        if prob is not None:
            raise SlotUnavailableError("slot unavailable")
        await _occupy(session, b)  # SlotTakenError if someone took it meanwhile [B5]
        b.cancelled_by = None
        b.cancel_reason = None
    else:
        raise _invalid(b)
    b.status = BookingStatus.CONFIRMED
    b.awaiting = None
    b.decided_at = now
    await session.flush()
    await notifications.schedule_for_booking(session, booking_id=b.id, now=now)
    return await _dto(session, b)


async def reject_booking(
    session: AsyncSession,
    *,
    booking_id: int,
    actor_user_id: int,
    reason: str | None,
    now: datetime | None = None,
) -> BookingDTO:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    if b.status == BookingStatus.REJECTED:
        return await _dto(session, b)
    if b.status != BookingStatus.PENDING or b.awaiting != Awaiting.BARBER:
        raise _invalid(b)
    b.status = BookingStatus.REJECTED
    b.cancel_reason = (reason or "").strip()[:200] or None
    b.decided_at = now
    await session.flush()
    await _release(session, b.id)
    await notifications.cancel_for_booking(session, booking_id=b.id, now=now)
    return await _dto(session, b)


async def propose_time(
    session: AsyncSession,
    *,
    booking_id: int,
    actor_user_id: int,
    new_start_at: datetime,
    now: datetime | None = None,
) -> ProposalResult:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    try:
        new_start_at = clock.ensure_aware(new_start_at)
    except ValueError:
        raise SlotUnavailableError("naive start") from None
    if (
        b.status == BookingStatus.PENDING
        and b.awaiting == Awaiting.CLIENT
        and b.start_at == new_start_at
    ):
        return ProposalResult(booking=await _dto(session, b))
    if b.status != BookingStatus.PENDING or b.awaiting != Awaiting.BARBER:
        raise _invalid(b)
    if (b.proposal_count or 0) >= 2:
        raise ProposalLimitError("proposal limit reached", limit=2)
    if new_start_at == b.start_at or not clock.is_on_grid(new_start_at):
        raise SlotUnavailableError("invalid proposal time")
    barber = await load_barber(session, b.barber_id)
    prob = await slot_problem(
        session,
        barber_id=b.barber_id,
        start_at=new_start_at,
        duration_min=b.duration_min,
        buffer_min=barber.buffer_min,
        now=now,
        ignore_booking_id=b.id,
        check_horizon=False,
    )
    if prob == "taken":
        raise SlotTakenError("slot taken")
    if prob is not None:
        raise SlotUnavailableError("slot unavailable")
    new_end = new_start_at + timedelta(minutes=b.duration_min)
    if await _client_overlaps(session, b, new_start_at, new_end):
        raise ClientLimitError("client has another booking then", kind="overlap", limit=0)
    s = await get_shop_settings(session)
    await _release(session, b.id)
    if b.original_start_at is None:
        b.original_start_at = b.start_at
    b.start_at = new_start_at
    b.end_at = new_start_at + timedelta(minutes=b.duration_min)
    b.block_end_at = b.end_at + timedelta(minutes=barber.buffer_min)
    b.proposal_count = (b.proposal_count or 0) + 1
    b.awaiting = Awaiting.CLIENT
    cutoff = new_start_at - timedelta(minutes=s.pending_cutoff_before_start_min)
    b.expire_at = max(
        min(now + timedelta(minutes=s.proposal_reply_min), cutoff),
        now + timedelta(minutes=s.min_decision_min),
    )
    await session.flush()
    await _occupy(session, b)
    await notifications.cancel_for_booking(
        session, booking_id=b.id, kinds=[NK.PENDING_REPING], now=now
    )
    return ProposalResult(booking=await _dto(session, b))


async def answer_proposal(
    session: AsyncSession,
    *,
    booking_id: int,
    client_user_id: int,
    accept: bool,
    now: datetime | None = None,
) -> BookingDTO:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    _client_actor(b, client_user_id)
    if accept and b.status == BookingStatus.CONFIRMED and b.proposal_count:
        return await _dto(session, b)
    if (
        not accept
        and b.status == BookingStatus.CANCELLED_BY_CLIENT
        and b.cancel_reason == "proposal_declined"
    ):
        return await _dto(session, b)
    if b.status != BookingStatus.PENDING or b.awaiting != Awaiting.CLIENT:
        raise _invalid(b)
    if accept:
        if (b.expire_at is not None and now >= b.expire_at) or b.start_at <= now:
            raise TooLateError("proposal expired")
        if await _client_overlaps(session, b, b.start_at, b.end_at):
            raise ClientLimitError("overlapping booking", kind="overlap", limit=0)
        b.status = BookingStatus.CONFIRMED
        b.awaiting = None
        b.decided_at = now
        await session.flush()
        await notifications.schedule_for_booking(session, booking_id=b.id, now=now)
    else:
        await _cancel(
            session,
            b,
            status=BookingStatus.CANCELLED_BY_CLIENT,
            by=CancelledBy.CLIENT,
            reason="proposal_declined",
            now=now,
        )
    return await _dto(session, b)


# ------------------------------------------------------------------ cancels


async def cancel_by_client(
    session: AsyncSession,
    *,
    booking_id: int,
    client_user_id: int,
    reason: str | None = None,
    now: datetime | None = None,
) -> CancelResult:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    _client_actor(b, client_user_id)
    if b.status == BookingStatus.CANCELLED_BY_CLIENT:
        return CancelResult(booking=await _dto(session, b), late=bool(b.late_cancel))
    if b.status not in ACTIVE_STATUSES:
        raise _invalid(b)
    if b.start_at <= now:
        raise TooLateError("booking already started")
    s = await get_shop_settings(session)
    late = b.status == BookingStatus.CONFIRMED and b.start_at - now < timedelta(
        hours=s.free_cancel_hours
    )
    if late:
        b.late_cancel = True
        if b.client_id is not None:
            await add_user_counter(session, b.client_id, "late_cancel_count", 1)
    await _cancel(
        session,
        b,
        status=BookingStatus.CANCELLED_BY_CLIENT,
        by=CancelledBy.CLIENT,
        reason=(reason or "").strip() or None,
        now=now,
    )
    return CancelResult(booking=await _dto(session, b), late=late)


async def cancel_by_staff(
    session: AsyncSession,
    *,
    booking_id: int,
    actor_user_id: int,
    reason: str,
    now: datetime | None = None,
) -> BookingDTO:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    if b.status == BookingStatus.CANCELLED_BY_BARBER:
        return await _dto(session, b)
    if b.status not in ACTIVE_STATUSES:
        raise _invalid(b)
    reason = (reason or "").strip()
    if not reason:
        raise InputError("reason required", field="reason")
    await _cancel(
        session,
        b,
        status=BookingStatus.CANCELLED_BY_BARBER,
        by=await _staff_by(session, actor_user_id, b),
        reason=reason,
        now=now,
    )
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="booking.staff_cancel",
        entity="booking",
        entity_id=b.id,
        payload={"reason": reason},
    )
    return await _dto(session, b)


# ------------------------------------------------------------------ completion / no-show


async def _complete(
    session: AsyncSession,
    b: Booking,
    *,
    auto: bool,
    now: datetime,
    actor_user_id: int | None = None,
) -> CompleteResult:
    b.status = BookingStatus.COMPLETED
    b.awaiting = None
    b.completed_at = now
    b.auto_completed = auto
    await session.flush()
    await _release(session, b.id)

    redeemed = False
    bonus_failed = False
    if b.bonus_cost > 0:
        if await _bonus.apply_redeem(session, booking_id=b.id):
            redeemed = True
            b.bonus_redeemed = True
        else:
            bonus_failed = True
            b.bonus_redeemed = False
            b.final_price_uzs = b.price_snapshot
        await session.flush()

    earned = welcome = 0
    referral_to: int | None = None
    needs_visit_confirm = False
    if b.source == BookingSource.MANUAL:
        needs_visit_confirm = b.client_id is not None
        await write_audit(
            session,
            actor_user_id=actor_user_id,
            action="booking.manual_complete",
            entity="booking",
            entity_id=b.id,
            payload={"auto": auto, "client_id": b.client_id},
        )
    else:
        earned, welcome, referral_to = await _bonus.award_for_completion(session, booking_id=b.id)

    await notifications.schedule_for_booking(session, booking_id=b.id, now=now)
    return CompleteResult(
        booking=await _dto(session, b),
        earned=earned,
        redeemed=redeemed,
        bonus_failed=bonus_failed,
        new_balance=await _client_balance(session, b.client_id),
        welcome_credited=welcome,
        referral_credited_to=referral_to,
        needs_visit_confirm=needs_visit_confirm,
    )


async def _unchanged_complete(session: AsyncSession, b: Booking) -> CompleteResult:
    return CompleteResult(
        booking=await _dto(session, b),
        earned=0,
        redeemed=bool(b.bonus_redeemed),
        bonus_failed=False,
        new_balance=await _client_balance(session, b.client_id),
        welcome_credited=0,
        referral_credited_to=None,
        needs_visit_confirm=False,
    )


async def complete_booking(
    session: AsyncSession, *, booking_id: int, actor_user_id: int, now: datetime | None = None
) -> CompleteResult:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    if b.status == BookingStatus.COMPLETED:
        return await _unchanged_complete(session, b)
    if b.status != BookingStatus.CONFIRMED:
        raise _invalid(b)
    if now < b.start_at - timedelta(minutes=30):
        raise TooEarlyError("too early to complete")
    return await _complete(session, b, auto=False, now=now, actor_user_id=actor_user_id)


async def mark_no_show(
    session: AsyncSession, *, booking_id: int, actor_user_id: int, now: datetime | None = None
) -> NoShowResult:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    await _barber_actor(session, actor_user_id, b)
    if b.status == BookingStatus.NO_SHOW:
        return NoShowResult(booking=await _dto(session, b), reversed=0)
    reversed_amount = 0
    if b.status == BookingStatus.CONFIRMED:
        if now < b.start_at + timedelta(minutes=15):
            raise TooEarlyError("too early for no-show")
        b.status = BookingStatus.NO_SHOW
        b.awaiting = None
        b.decided_at = b.decided_at or now
        await session.flush()
        await _release(session, b.id)
        await notifications.cancel_for_booking(session, booking_id=b.id, now=now)
    elif b.status == BookingStatus.COMPLETED:
        if not b.auto_completed:
            raise _invalid(b)
        s = await get_shop_settings(session)
        if b.completed_at is None or now > b.completed_at + timedelta(
            hours=s.noshow_flip_window_hours
        ):
            raise TooLateError("no-show flip window passed")
        reversed_amount = await _bonus.reverse_for_booking(session, booking_id=b.id)
        b.status = BookingStatus.NO_SHOW
        await session.execute(delete(Review).where(Review.booking_id == b.id))  # [D-3]
        await session.flush()
        await notifications.cancel_for_booking(
            session,
            booking_id=b.id,
            kinds=[NK.RETURN, NK.REVIEW, NK.MANUAL_VISIT_CONFIRM, NK.COMPLETED_THANKS],
            now=now,
        )
    else:
        raise _invalid(b)
    if b.client_id is not None:
        await add_user_counter(session, b.client_id, "no_show_count", 1)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="booking.no_show",
        entity="booking",
        entity_id=b.id,
        payload={"reversed": reversed_amount},
    )
    return NoShowResult(booking=await _dto(session, b), reversed=reversed_amount)


async def client_ack(
    session: AsyncSession, *, booking_id: int, client_user_id: int, now: datetime | None = None
) -> BookingDTO:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    _client_actor(b, client_user_id)
    if b.status != BookingStatus.CONFIRMED:
        raise _invalid(b)
    if b.client_ack_at is not None:
        return await _dto(session, b)
    if b.start_at <= now:
        raise TooLateError("booking already started")
    b.client_ack_at = now
    await session.flush()
    return await _dto(session, b)


async def confirm_manual_visit(
    session: AsyncSession,
    *,
    booking_id: int,
    client_user_id: int,
    yes: bool,
    now: datetime | None = None,
) -> CompleteResult:
    now = _now(now)
    b = await _load_locked(session, booking_id)
    _client_actor(b, client_user_id)
    if b.source != BookingSource.MANUAL or b.status != BookingStatus.COMPLETED:
        raise _invalid(b)
    if b.visit_confirmed is not None:
        return await _unchanged_complete(session, b)
    b.visit_confirmed = bool(yes)
    await session.flush()
    earned = welcome = 0
    referral_to: int | None = None
    if yes:
        earned, welcome, referral_to = await _bonus.award_for_completion(session, booking_id=b.id)
        # the visit is real now: the usual return / review pushes apply
        await notifications.schedule_for_booking(session, booking_id=b.id, now=now)
    else:
        await session.execute(delete(Review).where(Review.booking_id == b.id))  # [D-2]
        await notifications.cancel_for_booking(session, booking_id=b.id, now=now)
        await write_audit(
            session,
            actor_user_id=client_user_id,
            action="manual_visit_denied",
            entity="booking",
            entity_id=b.id,
            payload={"barber_id": b.barber_id},
        )
    await notifications.cancel_for_booking(
        session, booking_id=b.id, kinds=[NK.MANUAL_VISIT_CONFIRM], now=now
    )
    return CompleteResult(
        booking=await _dto(session, b),
        earned=earned,
        redeemed=bool(b.bonus_redeemed),
        bonus_failed=False,
        new_balance=await _client_balance(session, b.client_id),
        welcome_credited=welcome,
        referral_credited_to=referral_to,
        needs_visit_confirm=False,
    )


# ------------------------------------------------------------------ system jobs


async def expire_due_pending(
    session: AsyncSession, *, now: datetime | None = None
) -> list[BookingDTO]:
    """Internal: PENDING with expire_at <= now -> EXPIRED."""
    now = _now(now)
    rows = (
        (
            await session.execute(
                select(Booking)
                .where(
                    Booking.status == BookingStatus.PENDING,
                    Booking.expire_at.is_not(None),
                    Booking.expire_at <= now,
                )
                .order_by(Booking.expire_at, Booking.id)
            )
        )
        .scalars()
        .all()
    )
    for b in rows:
        await _cancel(
            session, b, status=BookingStatus.EXPIRED, by=CancelledBy.SYSTEM, reason=None, now=now
        )
        b.decided_at = None
    await session.flush()
    return await booking_dtos(session, rows)


async def auto_complete_due(
    session: AsyncSession, *, now: datetime | None = None
) -> list[CompleteResult]:
    """Internal: CONFIRMED -> COMPLETED (auto) once end_at + auto_complete_after_hours has
    passed AND the nightly digest that asks the barber first has run, plus
    DIGEST_ANSWER_WINDOW to answer it (§4.10): a morning visit is not closed before
    00:00 (digest 23:00 + 1 h) even if the hours have passed."""
    now = _now(now)
    s = await get_shop_settings(session)
    limit = now - timedelta(hours=s.auto_complete_after_hours)
    rows = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.status == BookingStatus.CONFIRMED, Booking.end_at <= limit)
                .order_by(Booking.end_at, Booking.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        await _complete(session, b, auto=True, now=now)
        for b in rows
        if _first_digest_after(b.end_at, s.nightly_check_time) + DIGEST_ANSWER_WINDOW <= now
    ]


# The barber gets this long to answer the nightly digest before its visits auto-complete.
DIGEST_ANSWER_WINDOW = timedelta(hours=1)


def _first_digest_after(end_at: datetime, nightly: time) -> datetime:
    """The first nightly digest run (local ``nightly`` time) at or after ``end_at``."""
    day = clock.to_local(end_at).date()
    at = clock.local_dt(day, nightly)
    if at < clock.ensure_aware(end_at):
        at = clock.local_dt(day + timedelta(days=1), nightly)
    return at


# ------------------------------------------------------------------ queries


async def get_booking(session: AsyncSession, booking_id: int) -> BookingDTO:
    """Internal, no actor check."""
    return await _dto(session, await load_booking(session, booking_id))


async def get_booking_for_actor(
    session: AsyncSession, *, booking_id: int, actor_user_id: int, now: datetime | None = None
) -> BookingDTO:
    now = _now(now)
    b = await load_booking(session, booking_id)
    if b.client_id is not None and b.client_id == actor_user_id:
        return await _dto(session, b)
    actor = await load_user(session, actor_user_id)
    roles = await roles_for(session, actor)
    if roles.is_admin:
        return await _dto(session, b)
    if roles.is_barber and roles.barber_id == b.barber_id:
        return await booking_dto(session, b, mask_before=phone_mask_cutoff(now))
    raise PermissionDeniedError("not allowed to view this booking")


async def list_client_bookings(
    session: AsyncSession,
    *,
    client_user_id: int,
    past_limit: int = 5,
    now: datetime | None = None,
) -> ClientBookings:
    now = _now(now)
    upcoming = (
        (
            await session.execute(
                select(Booking)
                .where(
                    Booking.client_id == client_user_id,
                    Booking.status.in_(_ACTIVE),
                    Booking.end_at > now,
                )
                .order_by(Booking.start_at, Booking.id)
            )
        )
        .scalars()
        .all()
    )
    up_ids = [b.id for b in upcoming]
    past_stmt = select(Booking).where(Booking.client_id == client_user_id)
    if up_ids:
        past_stmt = past_stmt.where(Booking.id.not_in(up_ids))
    past = (
        (
            await session.execute(
                past_stmt.order_by(Booking.start_at.desc(), Booking.id.desc()).limit(past_limit)
            )
        )
        .scalars()
        .all()
    )
    return ClientBookings(
        upcoming=await booking_dtos(session, upcoming), past=await booking_dtos(session, past)
    )


async def get_last_completed(session: AsyncSession, *, client_user_id: int) -> RebookHint | None:
    b = (
        await session.execute(
            select(Booking)
            .where(Booking.client_id == client_user_id, Booking.status == BookingStatus.COMPLETED)
            .order_by(Booking.start_at.desc(), Booking.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if b is None:
        return None
    barber = await session.get(Barber, b.barber_id)
    if barber is None or not await is_bookable(session, barber):
        return None
    service_id: int | None = None
    service_name: str | None = None
    if b.service_id is not None:
        svc = await session.get(Service, b.service_id)
        if svc is not None and svc.is_active:
            try:
                rs = await resolve_service(session, barber_id=barber.id, service_id=svc.id)
                service_id, service_name = rs.service_id, rs.name
            except InputError:
                pass
    return RebookHint(
        barber_id=barber.id,
        barber_name=barber.display_name,
        service_id=service_id,
        service_name=service_name,
    )


_DAY_DEFAULT = [
    BookingStatus.PENDING,
    BookingStatus.CONFIRMED,
    BookingStatus.COMPLETED,
    BookingStatus.NO_SHOW,
]


async def list_barber_day(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    day: date,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[BookingDTO]:
    now = _now(now)
    _, is_admin = await require_barber_or_admin(session, actor_user_id, barber_id)
    s, e = clock.day_bounds_utc(day)
    stmt = select(Booking).where(
        Booking.barber_id == barber_id, Booking.start_at >= s, Booking.start_at < e
    )
    if not include_inactive:
        stmt = stmt.where(Booking.status.in_(_DAY_DEFAULT))
    rows = (await session.execute(stmt.order_by(Booking.start_at, Booking.id))).scalars().all()
    return await booking_dtos(
        session, rows, mask_before=None if is_admin else phone_mask_cutoff(now)
    )


async def list_bookings(
    session: AsyncSession,
    *,
    actor_user_id: int,
    day: date | None = None,
    barber_id: int | None = None,
    status: BookingStatus | None = None,
    phone_or_name: str | None = None,
    page: int = 0,
    page_size: int = 10,
) -> Page[BookingDTO]:
    """Admin only [#76]."""
    await require_admin(session, actor_user_id)
    page_size = max(1, min(int(page_size), 50))
    conds = []
    if day is not None:
        s, e = clock.day_bounds_utc(day)
        conds += [Booking.start_at >= s, Booking.start_at < e]
    if barber_id is not None:
        conds.append(Booking.barber_id == barber_id)
    if status is not None:
        conds.append(Booking.status == BookingStatus(status))
    q = (phone_or_name or "").strip()
    if q:
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 4:
            conds.append(Booking.client_phone.like(f"%{digits}"))
        else:
            like = "%" + q.lower().replace("%", "").replace("_", "") + "%"
            # SQLite's lower() is ASCII-only; py_lower (app.db.session) handles Cyrillic
            lower = func.lower if await _is_postgres(session) else func.py_lower
            conds.append(lower(Booking.client_name).like(like))
    total = (
        await session.execute(select(func.count()).select_from(Booking).where(*conds))
    ).scalar_one()
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(int(page), pages - 1))
    order = (
        (Booking.start_at.asc(), Booking.id.asc())
        if day is not None
        else (Booking.start_at.desc(), Booking.id.desc())
    )
    rows = (
        (
            await session.execute(
                select(Booking)
                .where(*conds)
                .order_by(*order)
                .offset(page * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=await booking_dtos(session, rows), page=page, pages=pages, total=total)


async def list_unresolved(
    session: AsyncSession, *, barber_id: int | None = None, now: datetime | None = None
) -> list[BookingDTO]:
    """Internal: CONFIRMED with end_at < now."""
    now = _now(now)
    stmt = select(Booking).where(Booking.status == BookingStatus.CONFIRMED, Booking.end_at < now)
    if barber_id is not None:
        stmt = stmt.where(Booking.barber_id == barber_id)
    rows = (
        (await session.execute(stmt.order_by(Booking.barber_id, Booking.start_at))).scalars().all()
    )
    return await booking_dtos(session, rows)


async def list_pending_digest(
    session: AsyncSession, *, day: date, now: datetime | None = None
) -> list[PendingDigestItem]:
    """Internal: PENDING awaiting barber that start on ``day``."""
    now = _now(now)
    s, e = clock.day_bounds_utc(day)
    rows = (
        (
            await session.execute(
                select(Booking)
                .where(
                    Booking.status == BookingStatus.PENDING,
                    Booking.awaiting == Awaiting.BARBER,
                    Booking.start_at >= s,
                    Booking.start_at < e,
                    Booking.start_at > now,
                )
                .order_by(Booking.start_at, Booking.id)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for b in rows:
        out.append(
            PendingDigestItem(
                booking=await _dto(session, b), targets=await decision_targets(session, b)
            )
        )
    return out


async def bookings_for_daily_summary(
    session: AsyncSession, *, day: date
) -> dict[int, list[BookingDTO]]:
    """Internal: barber_id -> ACTIVE bookings of that local day."""
    s, e = clock.day_bounds_utc(day)
    rows = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.status.in_(_ACTIVE), Booking.start_at >= s, Booking.start_at < e)
                .order_by(Booking.barber_id, Booking.start_at)
            )
        )
        .scalars()
        .all()
    )
    out: dict[int, list[BookingDTO]] = {}
    for dto in await booking_dtos(session, rows):
        out.setdefault(dto.barber_id, []).append(dto)
    return out
