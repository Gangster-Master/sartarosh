"""Working hours, time off, busy-now, conflicts and shop hours (ARCHITECTURE.md §4.9)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ACTIVE_STATUSES, BookingStatus, CancelledBy
from app.db.models import Barber, Booking, TimeOff, WorkingHours
from app.services import clock
from app.services._common import (
    active_barber_of_user,
    booking_dtos,
    load_barber,
    load_user,
    require_admin,
    roles_for,
)
from app.services.audit import write_audit
from app.services.dto import (
    BookingDTO,
    HoursChangeResult,
    Interval,
    ShopDayHours,
    TimeOffDTO,
    TimeOffResult,
    WorkingHoursDTO,
)
from app.services.errors import InputError, NotFoundError, PermissionDeniedError
from app.services.settings import get_shop_settings

# ------------------------------------------------------------------ helpers (internal)


def _wh_dto(w: WorkingHours) -> WorkingHoursDTO:
    return WorkingHoursDTO(
        barber_id=w.barber_id,
        weekday=w.weekday,
        start_time=w.start_time,
        end_time=w.end_time,
        break_start=w.break_start,
        break_end=w.break_end,
    )


def _to_dto(t: TimeOff) -> TimeOffDTO:
    return TimeOffDTO(
        id=t.id, barber_id=t.barber_id, start_at=t.start_at, end_at=t.end_at, reason=t.reason
    )


async def hours_map(session: AsyncSession, barber_id: int) -> dict[int, WorkingHoursDTO]:
    """Internal: weekday -> hours for a barber."""
    rows = (
        (await session.execute(select(WorkingHours).where(WorkingHours.barber_id == barber_id)))
        .scalars()
        .all()
    )
    return {w.weekday: _wh_dto(w) for w in rows}


def day_windows(day: date, hours: WorkingHoursDTO | None) -> list[Interval]:
    """Working windows of one local day in UTC, the break cut out."""
    if hours is None:
        return []
    ws, we = clock.local_dt(day, hours.start_time), clock.local_dt(day, hours.end_time)
    if hours.break_start and hours.break_end:
        bs, be = clock.local_dt(day, hours.break_start), clock.local_dt(day, hours.break_end)
        out = []
        if ws < bs:
            out.append(Interval(ws, bs))
        if be < we:
            out.append(Interval(be, we))
        return out
    return [Interval(ws, we)]


async def barber_windows(
    session: AsyncSession, barber_id: int, start_day: date, end_day: date
) -> list[Interval]:
    """Internal: working windows (breaks excluded) for local days start_day..end_day inclusive."""
    hm = await hours_map(session, barber_id)
    out: list[Interval] = []
    d = start_day
    while d <= end_day:
        out.extend(day_windows(d, hm.get(d.weekday())))
        d += timedelta(days=1)
    return out


async def time_off_blocks(
    session: AsyncSession, barber_id: int | None, start: datetime, end: datetime
) -> list[Interval]:
    """Internal: barber's own and shop-wide time off overlapping [start, end)."""
    cond = TimeOff.barber_id.is_(None)
    if barber_id is not None:
        cond = or_(TimeOff.barber_id == barber_id, TimeOff.barber_id.is_(None))
    rows = (
        await session.execute(
            select(TimeOff.start_at, TimeOff.end_at).where(
                cond, TimeOff.start_at < end, TimeOff.end_at > start
            )
        )
    ).all()
    return [Interval(s, e) for s, e in rows]


async def booking_blocks(
    session: AsyncSession,
    barber_id: int,
    start: datetime,
    end: datetime,
    *,
    ignore_booking_id: int | None = None,
) -> list[Interval]:
    """Internal: ACTIVE bookings of the barber as [start_at, block_end_at) overlapping [start, end)."""
    stmt = select(Booking.start_at, Booking.block_end_at).where(
        Booking.barber_id == barber_id,
        Booking.status.in_(list(ACTIVE_STATUSES)),
        Booking.start_at < end,
        Booking.block_end_at > start,
    )
    if ignore_booking_id is not None:
        stmt = stmt.where(Booking.id != ignore_booking_id)
    return [Interval(s, e) for s, e in (await session.execute(stmt)).all()]


def _on_quarter(t: time | None, field: str) -> None:
    if t is None:
        return
    if t.minute % clock.SLOT_UNIT_MIN or t.second or t.microsecond:
        raise InputError("time must be on a 15-minute mark", field=field)


def _booking_fits(b: Booking, hours: WorkingHoursDTO | None) -> bool:
    day = clock.to_local(b.start_at).date()
    for w in day_windows(day, hours):
        if w.start <= b.start_at and b.end_at <= w.end:
            return True
    return False


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


async def _out_of_hours(
    session: AsyncSession, barber_id: int, weekdays: set[int], now: datetime
) -> list[Booking]:
    hm = await hours_map(session, barber_id)
    out = []
    for b in await _future_active(session, barber_id, now):
        wd = clock.to_local(b.start_at).weekday()
        if wd in weekdays and not _booking_fits(b, hm.get(wd)):
            out.append(b)
    return out


async def _can_manage_time_off(
    session: AsyncSession, actor_user_id: int, barber_id: int | None
) -> tuple[bool, bool]:
    """Return (is_admin, is_own_barber); raise if neither is allowed."""
    actor = await load_user(session, actor_user_id)
    roles = await roles_for(session, actor)
    if roles.is_admin:
        return True, roles.barber_id == barber_id and barber_id is not None
    if barber_id is None:
        raise PermissionDeniedError("shop-wide time off is admin only")
    if not roles.is_barber or roles.barber_id != barber_id:
        raise PermissionDeniedError("not your barber profile")
    if not (await get_shop_settings(session)).barbers_can_block_time:
        raise PermissionDeniedError("barbers may not block time", reason="setting")
    return False, True


async def _overlapping_active(
    session: AsyncSession, barber_id: int | None, start: datetime, end: datetime
) -> list[Booking]:
    stmt = select(Booking).where(
        Booking.status.in_(list(ACTIVE_STATUSES)),
        Booking.start_at < end,
        Booking.end_at > start,
    )
    if barber_id is not None:
        stmt = stmt.where(Booking.barber_id == barber_id)
    return list((await session.execute(stmt.order_by(Booking.start_at))).scalars())


def _norm_interval(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    try:
        s = clock.floor_to_grid(start_at)
        e = clock.ceil_to_grid(end_at)
    except ValueError:
        raise InputError("naive datetime", field="start_at") from None
    if e <= s:
        raise InputError("end must be after start", field="end_at")
    return s, e


# ------------------------------------------------------------------ public API


async def get_week_hours(session: AsyncSession, barber_id: int) -> list[WorkingHoursDTO]:
    await load_barber(session, barber_id)
    hm = await hours_map(session, barber_id)
    return [hm[k] for k in sorted(hm)]


async def get_shop_week_hours(session: AsyncSession) -> list[ShopDayHours]:
    """7 rows Mon..Sun; union over public barbers [#60]."""
    rows = (
        (
            await session.execute(
                select(WorkingHours)
                .join(Barber, Barber.id == WorkingHours.barber_id)
                .where(Barber.is_active.is_(True), Barber.user_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    out = []
    for wd in range(7):
        day_rows = [r for r in rows if r.weekday == wd]
        if not day_rows:
            out.append(ShopDayHours(weekday=wd, open_time=None, close_time=None))
        else:
            out.append(
                ShopDayHours(
                    weekday=wd,
                    open_time=min(r.start_time for r in day_rows),
                    close_time=max(r.end_time for r in day_rows),
                )
            )
    return out


async def shop_time_off_on(session: AsyncSession, day: date) -> list[Interval]:
    """Shop-wide time off (holidays) overlapping the local ``day``."""
    s, e = clock.day_bounds_utc(day)
    return await time_off_blocks(session, None, s, e)


async def set_working_hours(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    weekday: int,
    start_time: time | None,
    end_time: time | None,
    break_start: time | None = None,
    break_end: time | None = None,
    now: datetime | None = None,
) -> HoursChangeResult:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    await load_barber(session, barber_id)
    if not 0 <= int(weekday) <= 6:
        raise InputError("weekday 0..6", field="weekday")
    row = (
        await session.execute(
            select(WorkingHours).where(
                WorkingHours.barber_id == barber_id, WorkingHours.weekday == weekday
            )
        )
    ).scalar_one_or_none()
    dto: WorkingHoursDTO | None = None
    if start_time is None:
        if row is not None:
            await session.delete(row)
    else:
        if end_time is None:
            raise InputError("end time required", field="end_time")
        for t, f in (
            (start_time, "start_time"),
            (end_time, "end_time"),
            (break_start, "break_start"),
            (break_end, "break_end"),
        ):
            _on_quarter(t, f)
        if not start_time < end_time:
            raise InputError("start must be before end", field="end_time")
        if (break_start is None) != (break_end is None):
            raise InputError("break needs start and end", field="break")
        if break_start is not None and break_end is not None:
            if not (start_time <= break_start < break_end <= end_time):
                raise InputError("break outside hours", field="break")
        if row is None:
            row = WorkingHours(barber_id=barber_id, weekday=weekday)
            session.add(row)
        row.start_time, row.end_time = start_time, end_time
        row.break_start, row.break_end = break_start, break_end
    await session.flush()
    if row is not None and start_time is not None:
        dto = _wh_dto(row)
    out = await _out_of_hours(session, barber_id, {int(weekday)}, now)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="hours.set",
        entity="barber",
        entity_id=barber_id,
        payload={
            "weekday": weekday,
            "start": start_time,
            "end": end_time,
            "break_start": break_start,
            "break_end": break_end,
        },
    )
    return HoursChangeResult(hours=dto, out_of_hours=await booking_dtos(session, out))


async def copy_hours_to_all_weekdays(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    source_weekday: int,
    include_sunday: bool = False,
    now: datetime | None = None,
) -> list[BookingDTO]:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    await require_admin(session, actor_user_id)
    await load_barber(session, barber_id)
    hm = await hours_map(session, barber_id)
    src = hm.get(int(source_weekday))
    if src is None:  # a day off (or no such weekday): copying it would wipe the week
        raise InputError("source day has no hours", field="weekday")
    targets = [d for d in range(7 if include_sunday else 6) if d != source_weekday]
    for wd in targets:
        row = (
            await session.execute(
                select(WorkingHours).where(
                    WorkingHours.barber_id == barber_id, WorkingHours.weekday == wd
                )
            )
        ).scalar_one_or_none()
        if src is None:
            if row is not None:
                await session.delete(row)
            continue
        if row is None:
            row = WorkingHours(barber_id=barber_id, weekday=wd)
            session.add(row)
        row.start_time, row.end_time = src.start_time, src.end_time
        row.break_start, row.break_end = src.break_start, src.break_end
    await session.flush()
    out = await _out_of_hours(session, barber_id, set(targets), now)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="hours.copy",
        entity="barber",
        entity_id=barber_id,
        payload={"source_weekday": source_weekday, "include_sunday": include_sunday},
    )
    return await booking_dtos(session, out)


async def list_time_off(
    session: AsyncSession, *, barber_id: int | None, start: datetime, end: datetime
) -> list[TimeOffDTO]:
    """Barber: own + shop-wide entries; None: shop-wide only. Overlapping [start, end)."""
    start, end = clock.ensure_aware(start), clock.ensure_aware(end)
    cond = TimeOff.barber_id.is_(None)
    if barber_id is not None:
        cond = or_(TimeOff.barber_id == barber_id, TimeOff.barber_id.is_(None))
    rows = (
        (
            await session.execute(
                select(TimeOff)
                .where(cond, TimeOff.start_at < end, TimeOff.end_at > start)
                .order_by(TimeOff.start_at, TimeOff.id)
            )
        )
        .scalars()
        .all()
    )
    return [_to_dto(t) for t in rows]


async def preview_time_off_conflicts(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int | None,
    start_at: datetime,
    end_at: datetime,
) -> list[BookingDTO]:
    await _can_manage_time_off(session, actor_user_id, barber_id)
    if barber_id is not None:
        await load_barber(session, barber_id)
    s, e = _norm_interval(start_at, end_at)
    return await booking_dtos(session, await _overlapping_active(session, barber_id, s, e))


async def add_time_off(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int | None,
    start_at: datetime,
    end_at: datetime,
    reason: str | None,
    cancel_conflicts: bool,
    now: datetime | None = None,
) -> TimeOffResult:
    from app.services import bookings as _bookings  # bookings imports schedule

    now = clock.ensure_aware(now) if now else clock.now_utc()
    is_admin, _own = await _can_manage_time_off(session, actor_user_id, barber_id)
    if barber_id is not None:
        await load_barber(session, barber_id)
    s, e = _norm_interval(start_at, end_at)
    reason = (reason or "").strip()[:200] or None
    t = TimeOff(
        barber_id=barber_id,
        start_at=s,
        end_at=e,
        reason=reason,
        created_by_user_id=actor_user_id,
        created_at=now,
    )
    session.add(t)
    await session.flush()
    conflicts = await _overlapping_active(session, barber_id, s, e)
    cancelled: list[Booking] = []
    kept: list[Booking] = []
    for b in conflicts:
        if cancel_conflicts and b.start_at > now:
            by = CancelledBy.BARBER
            if is_admin:
                own_barber = await active_barber_of_user(session, actor_user_id)
                by = (
                    CancelledBy.BARBER
                    if own_barber and own_barber.id == b.barber_id
                    else CancelledBy.ADMIN
                )
            await _bookings._cancel(
                session,
                b,
                status=BookingStatus.CANCELLED_BY_BARBER,
                by=by,
                # the block's free-text reason is staff-internal; clients see a code
                reason=reason if reason in ("busy", "sick") else "time_off",
                now=now,
            )
            cancelled.append(b)
        else:
            kept.append(b)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="time_off.add",
        entity="time_off",
        entity_id=t.id,
        payload={
            "barber_id": barber_id,
            "start_at": s,
            "end_at": e,
            "cancelled": [b.id for b in cancelled],
        },
    )
    return TimeOffResult(
        time_off=_to_dto(t),
        cancelled=await booking_dtos(session, cancelled),
        overlapping_kept=await booking_dtos(session, kept),
    )


async def busy_now(
    session: AsyncSession, *, actor_user_id: int, minutes: int, now: datetime | None = None
) -> TimeOffResult:
    """Barber blocks [floor(now), ceil(now + minutes)); cancels nothing [F1]."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    if minutes not in (30, 60):
        raise InputError("minutes must be 30 or 60", field="minutes")
    actor = await load_user(session, actor_user_id)
    roles = await roles_for(session, actor)
    if not roles.is_barber or roles.barber_id is None:
        raise PermissionDeniedError("barbers only")
    if not roles.is_admin and not (await get_shop_settings(session)).barbers_can_block_time:
        raise PermissionDeniedError("barbers may not block time", reason="setting")
    s = clock.floor_to_grid(now)
    e = clock.ceil_to_grid(now + timedelta(minutes=minutes))
    t = TimeOff(
        barber_id=roles.barber_id,
        start_at=s,
        end_at=e,
        reason="busy",
        created_by_user_id=actor_user_id,
        created_at=now,
    )
    session.add(t)
    await session.flush()
    kept = await _overlapping_active(session, roles.barber_id, s, e)
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="time_off.busy_now",
        entity="time_off",
        entity_id=t.id,
        payload={"minutes": minutes},
    )
    return TimeOffResult(
        time_off=_to_dto(t), cancelled=[], overlapping_kept=await booking_dtos(session, kept)
    )


async def delete_time_off(session: AsyncSession, *, actor_user_id: int, time_off_id: int) -> None:
    t = await session.get(TimeOff, time_off_id)
    if t is None:
        raise NotFoundError("time off not found", entity="time_off", id=time_off_id)
    await _can_manage_time_off(session, actor_user_id, t.barber_id)
    payload = {"barber_id": t.barber_id, "start_at": t.start_at, "end_at": t.end_at}
    await session.delete(t)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="time_off.delete",
        entity="time_off",
        entity_id=time_off_id,
        payload=payload,
    )


def full_day_interval(day: date) -> Interval:
    s, e = clock.day_bounds_utc(day)
    return Interval(s, e)
