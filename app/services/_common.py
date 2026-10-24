"""Internal helpers shared by service modules (not part of the public contract).

Bot code must not import this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.enums import BookingSource, BookingStatus
from app.db.models import Barber, Booking, Review, User, WorkingHours
from app.services import clock
from app.services.dto import BarberDTO, BookingDTO, DecisionTarget, Roles, UserDTO
from app.services.errors import NotFoundError, PermissionDeniedError
from app.services.validation import mask_phone

# ---------------------------------------------------------------- users / roles


def user_dto(u: User) -> UserDTO:
    return UserDTO(
        id=u.id,
        tg_id=u.tg_id,
        tg_username=u.tg_username,
        first_name=u.first_name or "",
        full_name=u.full_name,
        display_name=u.full_name or u.first_name or "",
        phone=u.phone,
        phone_verified=bool(u.phone_verified),
        lang=u.lang or "uz",
        lang_chosen=bool(u.lang_chosen),
        consent_version=u.consent_version,
        is_admin=bool(u.is_admin),
        is_blocked=bool(u.is_blocked),
        no_show_count=u.no_show_count or 0,
        late_cancel_count=u.late_cancel_count or 0,
        bonus_balance=u.bonus_balance or 0,
        ref_code=u.ref_code,
        referrer_id=u.referrer_id,
        created_at=u.created_at,
    )


async def load_user(session: AsyncSession, user_id: int, *, fresh: bool = False) -> User:
    stmt = select(User).where(User.id == user_id)
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    u = (await session.execute(stmt)).scalar_one_or_none()
    if u is None:
        raise NotFoundError("user not found", entity="user", id=user_id)
    return u


async def active_barber_of_user(session: AsyncSession, user_id: int) -> Barber | None:
    return (
        await session.execute(
            select(Barber).where(Barber.user_id == user_id, Barber.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def roles_for(session: AsyncSession, u: User) -> Roles:
    is_env = u.tg_id in get_config().admin_ids and u.deleted_at is None
    barber = await active_barber_of_user(session, u.id) if u.deleted_at is None else None
    return Roles(
        is_client=True,
        is_barber=barber is not None,
        is_admin=is_env or bool(u.is_admin and u.deleted_at is None),
        is_env_admin=is_env,
        barber_id=barber.id if barber else None,
    )


async def require_admin(session: AsyncSession, actor_user_id: int) -> User:
    u = await load_user(session, actor_user_id)
    roles = await roles_for(session, u)
    if not roles.is_admin:
        raise PermissionDeniedError("admin only")
    return u


async def is_admin_user(session: AsyncSession, user_id: int) -> bool:
    try:
        u = await load_user(session, user_id)
    except NotFoundError:
        return False
    return (await roles_for(session, u)).is_admin


async def require_barber_or_admin(
    session: AsyncSession, actor_user_id: int, barber_id: int
) -> tuple[User, bool]:
    """Actor must be the linked user of ``barber_id`` or an admin. Returns (actor, is_admin)."""
    actor = await load_user(session, actor_user_id)
    roles = await roles_for(session, actor)
    if roles.is_admin:
        return actor, True
    if roles.is_barber and roles.barber_id == barber_id:
        return actor, False
    raise PermissionDeniedError("not your barber profile")


async def add_user_counter(session: AsyncSession, user_id: int, column: str, delta: int) -> None:
    """Atomic ``SET col = col + delta``."""
    col = getattr(User, column)
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values({column: col + delta})
        .execution_options(synchronize_session=False)
    )


async def admin_chat_ids(session: AsyncSession) -> list[tuple[int, str]]:
    """(tg_id, lang) for env admins and promoted admins; skips deleted/blocked users."""
    cfg = get_config()
    env_ids = cfg.admin_ids
    conds = [User.is_admin.is_(True)]
    if env_ids:
        conds.append(User.tg_id.in_(env_ids))
    rows = (
        (await session.execute(select(User).where(or_(*conds), User.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    out: dict[int, str] = {}
    for u in rows:
        if u.is_blocked:
            continue
        out[u.tg_id] = u.lang or cfg.default_lang
    for tg in sorted(env_ids):
        out.setdefault(tg, cfg.default_lang)
    return sorted(out.items())


# ---------------------------------------------------------------- barbers


def is_public(b: Barber) -> bool:
    """Active and linked. Clients also need working hours: see ``is_bookable``."""
    return bool(b.is_active and b.user_id is not None)


async def _ids_with_hours(session: AsyncSession, barber_ids: Iterable[int]) -> set[int]:
    ids = list(barber_ids)
    if not ids:
        return set()
    return set(
        (
            await session.execute(
                select(WorkingHours.barber_id).where(WorkingHours.barber_id.in_(ids)).distinct()
            )
        ).scalars()
    )


async def is_bookable(session: AsyncSession, b: Barber) -> bool:
    """Public for clients: active, linked and with at least one working day [D-1]."""
    return is_public(b) and bool(await _ids_with_hours(session, [b.id]))


def countable_review() -> list[Any]:
    """SQL conditions on Booking: the visit surely happened, so its review is public
    (COMPLETED, not auto-completed, a walk-in only after the client confirmed it)."""
    return [
        Booking.status == BookingStatus.COMPLETED,
        Booking.auto_completed.is_not(True),
        not_(and_(Booking.source == BookingSource.MANUAL, Booking.visit_confirmed.is_not(True))),
    ]


async def barber_dtos(session: AsyncSession, barbers: Sequence[Barber]) -> list[BarberDTO]:
    user_ids = {b.user_id for b in barbers if b.user_id is not None}
    tg: dict[int, int] = {}
    if user_ids:
        for uid, tg_id in (
            await session.execute(select(User.id, User.tg_id).where(User.id.in_(user_ids)))
        ).all():
            tg[uid] = tg_id
    ratings: dict[int, tuple[float, int]] = {}
    barber_ids = [b.id for b in barbers]
    if barber_ids:
        for bid, avg, cnt in (
            await session.execute(
                select(Review.barber_id, func.avg(Review.rating), func.count())
                .join(Booking, Booking.id == Review.booking_id)
                .where(Review.barber_id.in_(barber_ids), *countable_review())
                .group_by(Review.barber_id)
            )
        ).all():
            ratings[bid] = (round(float(avg), 1), int(cnt))
    with_hours = await _ids_with_hours(session, barber_ids)
    return [
        BarberDTO(
            id=b.id,
            user_id=b.user_id,
            tg_id=tg.get(b.user_id) if b.user_id is not None else None,
            display_name=b.display_name,
            photo_file_id=b.photo_file_id,
            bio=b.bio,
            is_active=bool(b.is_active),
            is_public=is_public(b) and b.id in with_hours,
            sort_order=b.sort_order or 0,
            slot_step_min=b.slot_step_min,
            buffer_min=b.buffer_min,
            auto_accept=bool(b.auto_accept),
            is_reachable=bool(b.is_reachable),
            invite_token=b.invite_token,
            invite_expires_at=b.invite_expires_at,
            pending_link_user_id=b.pending_link_user_id,
            rating_avg=ratings.get(b.id, (None, 0))[0],
            rating_count=ratings.get(b.id, (None, 0))[1],
        )
        for b in barbers
    ]


def has_hours() -> Any:
    """SQL condition: the barber has at least one working day (else clients can't book him)."""
    return select(WorkingHours.id).where(WorkingHours.barber_id == Barber.id).exists()


async def barber_dto(session: AsyncSession, b: Barber) -> BarberDTO:
    return (await barber_dtos(session, [b]))[0]


async def load_barber(session: AsyncSession, barber_id: int) -> Barber:
    b = await session.get(Barber, barber_id)
    if b is None:
        raise NotFoundError("barber not found", entity="barber", id=barber_id)
    return b


# ---------------------------------------------------------------- bookings


async def load_booking(session: AsyncSession, booking_id: int) -> Booking:
    b = await session.get(Booking, booking_id)
    if b is None:
        raise NotFoundError("booking not found", entity="booking", id=booking_id)
    return b


def phone_mask_cutoff(now: datetime | None = None) -> datetime:
    """Bookings starting before local (today - 1 day) show masked phones to barbers [H2]."""
    today = clock.local_today(now)
    return clock.day_bounds_utc(today - timedelta(days=1))[0]


async def booking_dtos(
    session: AsyncSession,
    bookings: Iterable[Booking],
    *,
    mask_before: datetime | None = None,
) -> list[BookingDTO]:
    """Build DTOs. If ``mask_before`` is set, phones of bookings starting earlier are masked."""
    items = list(bookings)
    if not items:
        return []
    client_ids = {b.client_id for b in items if b.client_id is not None}
    barber_ids = {b.barber_id for b in items}
    clients: dict[int, tuple[int, str]] = {}
    if client_ids:
        for uid, tg_id, lang in (
            await session.execute(
                select(User.id, User.tg_id, User.lang).where(User.id.in_(client_ids))
            )
        ).all():
            clients[uid] = (tg_id, lang)
    barbers: dict[int, tuple[str, int | None]] = {}
    for bid, name, uid in (
        await session.execute(
            select(Barber.id, Barber.display_name, Barber.user_id).where(Barber.id.in_(barber_ids))
        )
    ).all():
        barbers[bid] = (name, uid)
    barber_user_ids = {uid for _, uid in barbers.values() if uid is not None}
    barber_tg: dict[int, int] = {}
    if barber_user_ids:
        for uid, tg_id in (
            await session.execute(select(User.id, User.tg_id).where(User.id.in_(barber_user_ids)))
        ).all():
            barber_tg[uid] = tg_id
    default_lang = get_config().default_lang
    out: list[BookingDTO] = []
    for b in items:
        c = clients.get(b.client_id) if b.client_id is not None else None
        bname, buid = barbers.get(b.barber_id, ("", None))
        phone = b.client_phone
        if phone and mask_before is not None and b.start_at < mask_before:
            phone = mask_phone(phone)
        client_tg = c[0] if c else None
        if client_tg is not None and client_tg < 0:  # deleted user
            client_tg = None
        out.append(
            BookingDTO(
                id=b.id,
                status=b.status,
                awaiting=b.awaiting,
                source=b.source,
                client_id=b.client_id,
                client_tg_id=client_tg,
                client_lang=(c[1] if c else None) or default_lang,
                client_name=b.client_name,
                client_phone=phone,
                client_phone_verified=bool(b.client_phone_verified),
                barber_id=b.barber_id,
                barber_name=bname,
                barber_tg_id=barber_tg.get(buid) if buid is not None else None,
                service_id=b.service_id,
                service_name=b.service_name_snapshot,
                start_at=b.start_at,
                end_at=b.end_at,
                duration_min=b.duration_min,
                original_start_at=b.original_start_at,
                proposal_count=b.proposal_count or 0,
                price_snapshot=b.price_snapshot,
                final_price_uzs=b.final_price_uzs,
                bonus_cost=b.bonus_cost or 0,
                bonus_redeemed=bool(b.bonus_redeemed),
                comment=b.comment,
                expire_at=b.expire_at,
                late_cancel=bool(b.late_cancel),
                client_ack_at=b.client_ack_at,
                auto_completed=bool(b.auto_completed),
                completed_at=b.completed_at,
                visit_confirmed=b.visit_confirmed,
                cancelled_by=b.cancelled_by,
                cancel_reason=b.cancel_reason,
                created_at=b.created_at,
            )
        )
    return out


async def booking_dto(
    session: AsyncSession, b: Booking, *, mask_before: datetime | None = None
) -> BookingDTO:
    return (await booking_dtos(session, [b], mask_before=mask_before))[0]


async def decision_targets(
    session: AsyncSession, booking: Booking, *, needed_approval: bool | None = None
) -> list[DecisionTarget]:
    """Who gets accept/reject for a PENDING booking [#23].

    The barber, when linked and reachable. Admins are added when the barber is
    unreachable or unlinked, or when the barber has auto-accept on but the booking
    needed approval anyway (new or soft-blocked client).
    """
    barber = await load_barber(session, booking.barber_id)
    targets: list[DecisionTarget] = []
    barber_ok = False
    if barber.user_id is not None and barber.is_reachable:
        u = await session.get(User, barber.user_id)
        if u is not None and u.deleted_at is None:
            targets.append(DecisionTarget(chat_id=u.tg_id, lang=u.lang or "uz", role="barber"))
            barber_ok = True
    if needed_approval is None:
        needed_approval = bool(barber.auto_accept)
    if not barber_ok or needed_approval:
        seen = {t.chat_id for t in targets}
        for chat_id, lang in await admin_chat_ids(session):
            if chat_id not in seen:
                targets.append(DecisionTarget(chat_id=chat_id, lang=lang, role="admin"))
                seen.add(chat_id)
    return targets
