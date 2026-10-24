"""Barber CRUD, invite + approval linking, reachability (ARCHITECTURE.md §4.7)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ACTIVE_STATUSES, BookingStatus, CancelledBy
from app.db.models import Barber, Booking
from app.services import clock
from app.services._common import (
    barber_dto,
    barber_dtos,
    booking_dtos,
    load_barber,
    load_user,
    require_admin,
    user_dto,
)
from app.services.audit import write_audit
from app.services.dto import BarberDTO, BookingDTO, LinkRequest
from app.services.errors import InputError, InvalidInviteError

INVITE_TTL = timedelta(hours=24)
STEPS = (15, 30, 45, 60)
BUFFERS = (0, 15, 30)


def _check_display_name(v: str) -> str:
    name = " ".join((v or "").split())
    if not 1 <= len(name) <= 64:
        raise InputError("invalid name", field="display_name")
    return name


def _check_bio(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > 300:
        raise InputError("bio too long", field="bio", limit=300)
    return v or None


def _check_step(v: int) -> int:
    if v not in STEPS:
        raise InputError("invalid step", field="slot_step_min")
    return v


def _check_buffer(v: int) -> int:
    if v not in BUFFERS:
        raise InputError("invalid buffer", field="buffer_min")
    return v


def _new_token() -> str:
    return secrets.token_urlsafe(12)  # 16 chars


async def list_barbers(session: AsyncSession, *, public_only: bool = True) -> list[BarberDTO]:
    stmt = select(Barber)
    if public_only:
        stmt = stmt.where(Barber.is_active.is_(True), Barber.user_id.is_not(None))
    rows = (await session.execute(stmt.order_by(Barber.sort_order, Barber.id))).scalars().all()
    return await barber_dtos(session, rows)


async def get_barber(session: AsyncSession, barber_id: int) -> BarberDTO:
    return await barber_dto(session, await load_barber(session, barber_id))


async def get_barber_by_user(session: AsyncSession, user_id: int) -> BarberDTO | None:
    b = (
        await session.execute(select(Barber).where(Barber.user_id == user_id))
    ).scalar_one_or_none()
    return await barber_dto(session, b) if b else None


async def create_barber(
    session: AsyncSession,
    *,
    actor_user_id: int,
    display_name: str,
    bio: str | None = None,
    photo_file_id: str | None = None,
    slot_step_min: int = 30,
    buffer_min: int = 0,
    auto_accept: bool = True,
    now: datetime | None = None,
) -> BarberDTO:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    max_sort = (await session.execute(select(func.max(Barber.sort_order)))).scalar() or 0
    b = Barber(
        display_name=_check_display_name(display_name),
        bio=_check_bio(bio),
        photo_file_id=(photo_file_id or None),
        slot_step_min=_check_step(slot_step_min),
        buffer_min=_check_buffer(buffer_min),
        auto_accept=bool(auto_accept),
        is_active=True,
        is_reachable=True,
        sort_order=max_sort + 1,
        invite_token=_new_token(),
        invite_expires_at=now + INVITE_TTL,
        created_at=now,
        updated_at=now,
    )
    session.add(b)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.create",
        entity="barber",
        entity_id=b.id,
        payload={"display_name": b.display_name},
    )
    return await barber_dto(session, b)


async def update_barber(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    display_name: str | None = None,
    bio: str | None = None,
    photo_file_id: str | None = None,
    slot_step_min: int | None = None,
    buffer_min: int | None = None,
    auto_accept: bool | None = None,
    sort_order: int | None = None,
) -> BarberDTO:
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    changes: dict[str, object] = {}
    if display_name is not None:
        b.display_name = changes["display_name"] = _check_display_name(display_name)
    if bio is not None:
        b.bio = changes["bio"] = _check_bio(bio)
    if photo_file_id is not None:
        b.photo_file_id = changes["photo_file_id"] = photo_file_id or None
    if slot_step_min is not None:
        b.slot_step_min = changes["slot_step_min"] = _check_step(slot_step_min)
    if buffer_min is not None:
        b.buffer_min = changes["buffer_min"] = _check_buffer(buffer_min)
    if auto_accept is not None:
        b.auto_accept = changes["auto_accept"] = bool(auto_accept)
    if sort_order is not None:
        b.sort_order = changes["sort_order"] = int(sort_order)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.update",
        entity="barber",
        entity_id=b.id,
        payload=changes,
    )
    return await barber_dto(session, b)


async def regenerate_invite(
    session: AsyncSession, *, actor_user_id: int, barber_id: int, now: datetime | None = None
) -> BarberDTO:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    b.invite_token = _new_token()
    b.invite_expires_at = now + INVITE_TTL
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.invite",
        entity="barber",
        entity_id=b.id,
    )
    return await barber_dto(session, b)


async def request_link(
    session: AsyncSession, *, user_id: int, token: str, now: datetime | None = None
) -> LinkRequest:
    """Consume the invite; the link waits for admin approval [D4]."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    if not token:
        raise InvalidInviteError("invalid invite")
    b = (
        await session.execute(select(Barber).where(Barber.invite_token == token))
    ).scalar_one_or_none()
    if b is None or b.invite_expires_at is None or b.invite_expires_at <= now:
        raise InvalidInviteError("invalid or expired invite")
    if b.user_id is not None:
        raise InvalidInviteError("barber already linked")
    user = await load_user(session, user_id)
    if user.deleted_at is not None:
        raise InvalidInviteError("user deleted")
    other = (
        await session.execute(select(Barber.id).where(Barber.user_id == user_id))
    ).scalar_one_or_none()
    if other is not None:
        raise InvalidInviteError("user already a barber")
    b.invite_token = None
    b.invite_expires_at = None
    b.pending_link_user_id = user_id
    await session.flush()
    await write_audit(
        session,
        actor_user_id=user_id,
        action="barber.link_request",
        entity="barber",
        entity_id=b.id,
    )
    return LinkRequest(barber=await barber_dto(session, b), user=user_dto(user))


async def approve_link(
    session: AsyncSession, *, actor_user_id: int, barber_id: int, user_id: int
) -> BarberDTO:
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    if b.pending_link_user_id is None or b.pending_link_user_id != user_id:
        raise InvalidInviteError("no such link request")
    if b.user_id is not None:
        raise InvalidInviteError("barber already linked")
    other = (
        await session.execute(select(Barber.id).where(Barber.user_id == user_id))
    ).scalar_one_or_none()
    if other is not None:
        raise InvalidInviteError("user already a barber")
    b.user_id = user_id
    b.pending_link_user_id = None
    b.is_reachable = True
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.link_approve",
        entity="barber",
        entity_id=b.id,
        payload={"user_id": user_id},
    )
    return await barber_dto(session, b)


async def deny_link(
    session: AsyncSession, *, actor_user_id: int, barber_id: int, user_id: int
) -> BarberDTO:
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    if b.pending_link_user_id is None or b.pending_link_user_id != user_id:
        raise InvalidInviteError("no such link request")
    b.pending_link_user_id = None
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.link_deny",
        entity="barber",
        entity_id=b.id,
        payload={"user_id": user_id},
    )
    return await barber_dto(session, b)


async def unlink_barber(session: AsyncSession, *, actor_user_id: int, barber_id: int) -> BarberDTO:
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    old = b.user_id
    b.user_id = None
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.unlink",
        entity="barber",
        entity_id=b.id,
        payload={"user_id": old},
    )
    return await barber_dto(session, b)


async def _future_active(session: AsyncSession, barber_id: int, now: datetime) -> list[Booking]:
    return list(
        (
            await session.execute(
                select(Booking)
                .where(
                    Booking.barber_id == barber_id,
                    Booking.status.in_(list(ACTIVE_STATUSES)),
                    Booking.start_at > now,
                )
                .order_by(Booking.start_at)
            )
        ).scalars()
    )


async def preview_future_bookings(
    session: AsyncSession, *, actor_user_id: int, barber_id: int, now: datetime | None = None
) -> list[BookingDTO]:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    await load_barber(session, barber_id)
    return await booking_dtos(session, await _future_active(session, barber_id, now))


async def deactivate_barber(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    reason: str,
    now: datetime | None = None,
) -> list[BookingDTO]:
    """Always cancels all future ACTIVE bookings (CANCELLED_BY_BARBER, by ADMIN) [B6]."""
    from app.services import bookings as _bookings

    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    reason = (reason or "").strip()
    if not reason:
        raise InputError("reason required", field="reason")
    b = await load_barber(session, barber_id)
    rows = await _future_active(session, barber_id, now)
    for bk in rows:
        await _bookings._cancel(
            session,
            bk,
            status=BookingStatus.CANCELLED_BY_BARBER,
            by=CancelledBy.ADMIN,
            reason="barber_inactive",  # the admin's own text stays in the audit log only
            now=now,
        )
    b.is_active = False
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.deactivate",
        entity="barber",
        entity_id=b.id,
        payload={"reason": reason, "cancelled": [x.id for x in rows]},
    )
    return await booking_dtos(session, rows)


async def activate_barber(
    session: AsyncSession, *, actor_user_id: int, barber_id: int
) -> BarberDTO:
    await require_admin(session, actor_user_id)
    b = await load_barber(session, barber_id)
    b.is_active = True
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.activate",
        entity="barber",
        entity_id=b.id,
    )
    return await barber_dto(session, b)


async def set_reachable(session: AsyncSession, *, barber_id: int, reachable: bool) -> None:
    """Internal."""
    b = await load_barber(session, barber_id)
    if b.is_reachable != reachable:
        b.is_reachable = reachable
        await session.flush()


async def mark_reachable_by_user(session: AsyncSession, *, user_id: int) -> bool:
    """Internal: if that user's barber is unreachable, set it reachable and return True [B4]."""
    b = (
        await session.execute(select(Barber).where(Barber.user_id == user_id))
    ).scalar_one_or_none()
    if b is None or b.is_reachable:
        return False
    b.is_reachable = True
    await session.flush()
    return True
