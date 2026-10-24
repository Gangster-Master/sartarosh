"""Users, roles, consent, contact, referral codes and admin client ops (ARCHITECTURE.md §4.6)."""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.enums import BookingStatus
from app.db.models import Barber, Booking, Setting, User
from app.services import clock
from app.services._common import (
    active_barber_of_user,
    admin_chat_ids,
    booking_dtos,
    load_user,
    require_admin,
    roles_for,
    user_dto,
)
from app.services.audit import write_audit
from app.services.bonus import _same_person_ids
from app.services.dto import (
    BookingDTO,
    ClientProfile,
    ClientStats,
    ContactResult,
    Roles,
    UserContext,
    UserDTO,
)
from app.services.errors import (
    InputError,
    InvalidInviteError,
    InvalidPhoneError,
    NotFoundError,
    PermissionDeniedError,
)
from app.services.settings import SETUP_TOKEN_KEY, get_marker, get_shop_settings
from app.services.validation import normalize_contact_phone, validate_name

_REF_ALPHABET = string.ascii_lowercase + string.digits
_SRC_RE = re.compile(r"^[a-z0-9_]{1,32}$")
LANGS = ("uz", "ru")


async def _context(session: AsyncSession, u: User) -> UserContext:
    roles = await roles_for(session, u)
    reachable: bool | None = None
    if roles.is_barber:
        b = await active_barber_of_user(session, u.id)
        reachable = bool(b.is_reachable) if b else None
    return UserContext(user=user_dto(u), roles=roles, barber_reachable=reachable)


async def load_context(session: AsyncSession, *, tg_id: int) -> UserContext | None:
    u = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if u is None:
        return None
    return await _context(session, u)


def needs_sync(user: UserDTO, *, tg_username: str | None, first_name: str) -> bool:
    return user.tg_username != (tg_username or None) or user.first_name != (first_name or "")[:128]


async def _new_ref_code(session: AsyncSession) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))
        exists = (
            await session.execute(select(User.id).where(User.ref_code == code))
        ).scalar_one_or_none()
        if exists is None:
            return code
    raise RuntimeError("could not generate a unique ref_code")


async def ensure_user(
    session: AsyncSession,
    *,
    tg_id: int,
    tg_username: str | None,
    first_name: str,
    language_code: str | None = None,
    start_payload: str | None = None,
) -> UserContext:
    first_name = (first_name or "")[:128]
    tg_username = (tg_username or None) and tg_username[:64]
    u = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if u is None:
        now = clock.now_utc()
        lang = "ru" if (language_code or "").lower().startswith("ru") else "uz"
        u = User(
            tg_id=tg_id,
            tg_username=tg_username,
            first_name=first_name,
            lang=lang,
            lang_chosen=False,
            ref_code=await _new_ref_code(session),
            created_at=now,
            updated_at=now,
        )
        payload = (start_payload or "").strip()
        if payload.startswith("ref_"):
            code = payload[4:].lower()
            ref = (
                await session.execute(
                    select(User).where(User.ref_code == code, User.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if ref is not None and ref.tg_id != tg_id:
                u.referrer_id = ref.id
        elif payload.startswith("src_"):
            tag = payload[4:].lower()
            if _SRC_RE.match(tag):
                u.acq_source = tag
        session.add(u)
        await session.flush()
    else:
        changed = False
        if u.tg_username != tg_username:
            u.tg_username = tg_username
            changed = True
        if u.first_name != first_name:
            u.first_name = first_name
            changed = True
        if changed:
            await session.flush()
    return await _context(session, u)


async def get_user(session: AsyncSession, user_id: int) -> UserDTO:
    return user_dto(await load_user(session, user_id, fresh=True))


async def get_roles(session: AsyncSession, user_id: int) -> Roles:
    return await roles_for(session, await load_user(session, user_id))


async def set_lang(session: AsyncSession, *, user_id: int, lang: str) -> UserDTO:
    if lang not in LANGS:
        raise InputError("unsupported language", field="lang")
    u = await load_user(session, user_id)
    u.lang = lang
    u.lang_chosen = True
    await session.flush()
    return user_dto(u)


async def accept_consent(session: AsyncSession, *, user_id: int, version: int) -> UserDTO:
    u = await load_user(session, user_id)
    if u.consent_version is None or u.consent_version < version:
        u.consent_version = version
        await session.flush()
    return user_dto(u)


async def set_full_name(session: AsyncSession, *, user_id: int, name: str) -> UserDTO:
    clean = validate_name(name)
    u = await load_user(session, user_id)
    u.full_name = clean[:64]
    await session.flush()
    return user_dto(u)


async def set_verified_contact(
    session: AsyncSession, *, user_id: int, raw_phone: str
) -> ContactResult:
    phone, is_foreign = normalize_contact_phone(raw_phone)
    if is_foreign and not (await get_shop_settings(session)).allow_foreign_phone:
        raise InvalidPhoneError("foreign phone not allowed", field="phone", reason="foreign")
    u = await load_user(session, user_id)
    u.phone = phone
    u.phone_verified = True
    await session.flush()
    dups = (
        (
            await session.execute(
                select(User.id).where(
                    User.phone == phone, User.id != user_id, User.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return ContactResult(user=user_dto(u), is_foreign=is_foreign, duplicate_user_ids=list(dups))


async def is_soft_blocked(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> bool:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    s = await get_shop_settings(session)
    if s.soft_block_no_shows <= 0:
        return False
    u = await load_user(session, user_id)
    since = now - timedelta(days=s.soft_block_window_days)
    if u.no_show_reset_at is not None and u.no_show_reset_at > since:
        since = u.no_show_reset_at
    # deleted accounts with the same verified phone count too (no /delete_me + rejoin reset)
    person_ids = await _same_person_ids(session, u)
    no_shows = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.client_id.in_(person_ids),
                Booking.status == BookingStatus.NO_SHOW,
                Booking.start_at >= since,
            )
        )
    ).scalar_one()
    score = no_shows
    if s.late_cancels_per_no_show > 0:
        # Late cancels are counted inside the same window (and after a reset), so
        # reset_no_shows clears them too.
        late = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(
                    Booking.client_id.in_(person_ids),
                    Booking.late_cancel.is_(True),
                    Booking.start_at >= since,
                )
            )
        ).scalar_one()
        score += late // s.late_cancels_per_no_show
    return score >= s.soft_block_no_shows


async def get_client_stats(session: AsyncSession, user_id: int) -> ClientStats:
    u = await load_user(session, user_id, fresh=True)
    completed = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.client_id == user_id, Booking.status == BookingStatus.COMPLETED)
        )
    ).scalar_one()
    return ClientStats(
        total_completed=completed,
        no_show_count=u.no_show_count or 0,
        late_cancel_count=u.late_cancel_count or 0,
        is_new=completed == 0,
    )


async def claim_setup_token(session: AsyncSession, *, user_id: int, token: str) -> UserDTO:
    expected = await get_marker(session, SETUP_TOKEN_KEY)
    if not expected or not token or not secrets.compare_digest(expected, token):
        raise InvalidInviteError("invalid setup token")
    u = await load_user(session, user_id)
    u.is_admin = True
    row = await session.get(Setting, SETUP_TOKEN_KEY)
    if row is not None:
        await session.delete(row)
    await session.flush()
    await write_audit(
        session, actor_user_id=user_id, action="admin.setup", entity="user", entity_id=user_id
    )
    return user_dto(u)


async def delete_me(
    session: AsyncSession, *, user_id: int, now: datetime | None = None
) -> list[BookingDTO]:
    from app.services import bookings as _bookings  # local import: bookings imports users

    now = clock.ensure_aware(now) if now else clock.now_utc()
    u = await load_user(session, user_id)
    active = await _bookings._active_bookings_of_client(session, user_id)
    cancelled_rows = []
    for b in active:
        await _bookings._cancel(
            session,
            b,
            status=BookingStatus.CANCELLED_BY_CLIENT,
            by=_bookings.CancelledBy.CLIENT,
            reason="deleted",
            now=now,
        )
        cancelled_rows.append(b)
    # A barber leaving: his clients' future bookings can't happen; cancel them like a
    # deactivation does (the caller tells the clients and the admins).
    from app.services.barbers import _future_active

    for barber in (
        await session.execute(select(Barber).where(Barber.user_id == user_id))
    ).scalars():
        for b in await _future_active(session, barber.id, now):
            await _bookings._cancel(
                session,
                b,
                status=BookingStatus.CANCELLED_BY_BARBER,
                by=_bookings.CancelledBy.BARBER,
                reason="barber_inactive",
                now=now,
            )
            cancelled_rows.append(b)
    result = await booking_dtos(session, cancelled_rows)

    # Unlink a barber profile, if any (keeps the barber row and history).
    for b in (await session.execute(select(Barber).where(Barber.user_id == user_id))).scalars():
        b.user_id = None
    for b in (
        await session.execute(select(Barber).where(Barber.pending_link_user_id == user_id))
    ).scalars():
        b.pending_link_user_id = None

    from app.services.validation import phone_hash

    if u.phone and u.phone_verified:
        u.deleted_phone_hash = phone_hash(u.phone)
    u.tg_id = -u.id
    u.tg_username = None
    u.full_name = None
    u.phone = None
    u.phone_verified = False
    u.first_name = ""
    u.is_admin = False
    u.deleted_at = now
    for b in (await session.execute(select(Booking).where(Booking.client_id == user_id))).scalars():
        b.client_name = "—"
        b.client_phone = None
    await session.flush()
    await write_audit(
        session, actor_user_id=user_id, action="user.delete_me", entity="user", entity_id=user_id
    )
    return result


# ------------------------------------------------------------------ admin


async def find_users_by_phone(
    session: AsyncSession, *, actor_user_id: int, phone_query: str
) -> list[UserDTO]:
    await require_admin(session, actor_user_id)
    digits = re.sub(r"\D", "", phone_query or "")
    if len(digits) < 4:
        raise InputError("at least 4 digits", field="phone")
    rows = (
        (
            await session.execute(
                select(User)
                .where(User.phone.like(f"%{digits}"), User.deleted_at.is_(None))
                .order_by(User.id)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="client.search",
        entity="user",
        entity_id=None,
        payload={"query": digits, "results": len(rows)},
    )
    return [user_dto(u) for u in rows]


async def get_client_profile(
    session: AsyncSession, *, actor_user_id: int, user_id: int
) -> ClientProfile:
    from app.services import bonus as _bonus

    await require_admin(session, actor_user_id)
    u = await load_user(session, user_id, fresh=True)
    recent = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.client_id == user_id)
                .order_by(Booking.start_at.desc(), Booking.id.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    return ClientProfile(
        user=user_dto(u),
        stats=await get_client_stats(session, user_id),
        recent_bookings=await booking_dtos(session, recent),
        bonus=await _bonus.get_bonus_status(session, user_id),
        soft_blocked=await is_soft_blocked(session, user_id),
    )


async def set_blocked(
    session: AsyncSession,
    *,
    actor_user_id: int,
    user_id: int,
    blocked: bool,
    now: datetime | None = None,
) -> list[BookingDTO]:
    from app.services import bookings as _bookings

    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    if user_id == actor_user_id:
        raise PermissionDeniedError("cannot block self", reason="self")
    u = await load_user(session, user_id)
    roles = await roles_for(session, u)
    if blocked and (roles.is_admin or roles.is_barber):
        raise PermissionDeniedError("cannot block staff", reason="staff")
    cancelled: list[BookingDTO] = []
    if blocked and not u.is_blocked:
        rows = await _bookings._active_bookings_of_client(session, user_id)
        for b in rows:
            await _bookings._cancel(
                session,
                b,
                status=BookingStatus.CANCELLED_BY_BARBER,
                by=_bookings.CancelledBy.ADMIN,
                reason="blocked",
                now=now,
            )
        cancelled = await booking_dtos(session, rows)
    u.is_blocked = blocked
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="client.block" if blocked else "client.unblock",
        entity="user",
        entity_id=user_id,
        payload={"cancelled": [b.id for b in cancelled]},
    )
    return cancelled


async def reset_no_shows(
    session: AsyncSession, *, actor_user_id: int, user_id: int, now: datetime | None = None
) -> UserDTO:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    u = await load_user(session, user_id)
    u.no_show_reset_at = now
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="client.reset_no_shows",
        entity="user",
        entity_id=user_id,
    )
    return user_dto(u)


async def set_admin(
    session: AsyncSession, *, actor_user_id: int, user_id: int, is_admin: bool
) -> UserDTO:
    await require_admin(session, actor_user_id)
    u = await load_user(session, user_id)
    if u.deleted_at is not None:
        raise NotFoundError("user deleted", entity="user", id=user_id)
    if is_admin and u.is_blocked:  # staff can't be blocked, so the blocked can't be staff [A7]
        raise InputError("user is blocked", field="blocked_user")
    if not is_admin:
        if u.tg_id in get_config().admin_ids:
            raise InputError("env admin cannot be demoted", field="env_admin")
        if user_id == actor_user_id:
            raise InputError("cannot demote self", field="self")
    u.is_admin = is_admin
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="admin.promote" if is_admin else "admin.demote",
        entity="user",
        entity_id=user_id,
    )
    return user_dto(u)


async def list_admins(session: AsyncSession, *, actor_user_id: int) -> list[UserDTO]:
    await require_admin(session, actor_user_id)
    env_ids = get_config().admin_ids
    conds = [User.is_admin.is_(True)]
    if env_ids:
        conds.append(User.tg_id.in_(env_ids))
    rows = (
        (
            await session.execute(
                select(User).where(or_(*conds), User.deleted_at.is_(None)).order_by(User.id)
            )
        )
        .scalars()
        .all()
    )
    return [user_dto(u) for u in rows]


async def list_admin_chat_ids(session: AsyncSession) -> list[tuple[int, str]]:
    """Internal: (tg_id, lang) of env and promoted admins."""
    return await admin_chat_ids(session)
