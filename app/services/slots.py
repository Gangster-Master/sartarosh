"""Free-slot computation: pure algorithm plus DB wrappers (ARCHITECTURE.md §4.10)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ACTIVE_STATUSES
from app.db.models import Barber, Booking
from app.services import clock
from app.services._common import is_public, load_booking, require_barber_or_admin
from app.services.catalog import _barbers_for_service_rows, resolve_service
from app.services.dto import DayAvailability, Interval, WorkingHoursDTO
from app.services.errors import InputError
from app.services.schedule import booking_blocks, day_windows, hours_map, time_off_blocks
from app.services.settings import get_shop_settings


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def compute_free_slots(
    *,
    day: date,
    hours: WorkingHoursDTO | None,
    blocks: list[Interval],
    duration_min: int,
    buffer_min: int,
    step_min: int,
    now: datetime,
    min_lead_min: int,
    last_bookable_day: date,
) -> list[datetime]:
    """Free start times (aware UTC, ascending, on grid) for one local day [#6][F4]."""
    if hours is None or day < clock.local_today(now) or day > last_bookable_day:
        return []
    ws = clock.local_dt(day, hours.start_time)
    we = clock.local_dt(day, hours.end_time)
    all_blocks = list(blocks)
    if hours.break_start is not None and hours.break_end is not None:
        all_blocks.append(
            Interval(clock.local_dt(day, hours.break_start), clock.local_dt(day, hours.break_end))
        )
    earliest = clock.ceil_to_grid(now + timedelta(minutes=min_lead_min))
    step = timedelta(minutes=step_min)
    duration = timedelta(minutes=duration_min)
    occ = duration + timedelta(minutes=buffer_min)

    cands: set[datetime] = set()
    t = ws
    while t < we:
        cands.add(t)
        t += step
    for b in all_blocks:
        if ws <= b.end < we:
            cands.add(b.end)

    out: list[datetime] = []
    for t in sorted(cands):
        if t < earliest or t + duration > we or not clock.is_on_grid(t):
            continue
        occ_end = t + occ
        if any(_overlap(t, occ_end, b.start, b.end) for b in all_blocks):
            continue
        out.append(t)
    return out


# ------------------------------------------------------------------ DB wrappers


async def _blocks_for_day(
    session: AsyncSession, barber_id: int, day: date, *, ignore_booking_id: int | None = None
) -> list[Interval]:
    s, e = clock.day_bounds_utc(day)
    # widen by a day on each side so buffers/time off crossing midnight are seen
    s, e = s - timedelta(days=1), e + timedelta(days=1)
    return await booking_blocks(
        session, barber_id, s, e, ignore_booking_id=ignore_booking_id
    ) + await time_off_blocks(session, barber_id, s, e)


async def _free_for_barber(
    session: AsyncSession,
    barber: Barber,
    service_id: int | None,
    day: date,
    now: datetime,
    *,
    ignore_booking_id: int | None = None,
    last_bookable_day: date | None = None,
) -> list[datetime]:
    try:
        rs = await resolve_service(session, barber_id=barber.id, service_id=service_id)
    except InputError:
        return []
    s = await get_shop_settings(session)
    if last_bookable_day is None:
        last_bookable_day = clock.local_today(now) + timedelta(days=s.booking_horizon_days - 1)
    hm = await hours_map(session, barber.id)
    hours = hm.get(day.weekday())
    if hours is None:
        return []
    blocks = await _blocks_for_day(session, barber.id, day, ignore_booking_id=ignore_booking_id)
    return compute_free_slots(
        day=day,
        hours=hours,
        blocks=blocks,
        duration_min=rs.duration_min,
        buffer_min=barber.buffer_min,
        step_min=barber.slot_step_min,
        now=now,
        min_lead_min=s.min_lead_min,
        last_bookable_day=last_bookable_day,
    )


async def get_free_slots(
    session: AsyncSession,
    *,
    barber_id: int,
    service_id: int | None,
    day: date,
    now: datetime | None = None,
) -> list[datetime]:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    barber = await session.get(Barber, barber_id)
    if barber is None or not is_public(barber):
        return []
    return await _free_for_barber(session, barber, service_id, day, now)


async def get_free_slots_any(
    session: AsyncSession, *, service_id: int | None, day: date, now: datetime | None = None
) -> list[datetime]:
    """Sorted union over the public barbers offering the service [#1]."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    out: set[datetime] = set()
    for b in await _barbers_for_service_rows(session, service_id):
        out.update(await _free_for_barber(session, b, service_id, day, now))
    return sorted(out)


async def get_available_days(
    session: AsyncSession,
    *,
    barber_id: int | None,
    service_id: int | None,
    now: datetime | None = None,
) -> list[DayAvailability]:
    """today .. today + horizon - 1; barber_id None = any barber."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    s = await get_shop_settings(session)
    today = clock.local_today(now)
    if barber_id is None:
        barbers = await _barbers_for_service_rows(session, service_id)
    else:
        b = await session.get(Barber, barber_id)
        barbers = [b] if b is not None and is_public(b) else []
    hours = {b.id: await hours_map(session, b.id) for b in barbers}
    out: list[DayAvailability] = []
    for i in range(s.booking_horizon_days):
        day = today + timedelta(days=i)
        working = False
        has_free = False
        for b in barbers:
            h = hours[b.id].get(day.weekday())
            if h is None:
                continue
            # a day fully covered by time off is not a working day
            ds, de = clock.day_bounds_utc(day)
            offs = await time_off_blocks(session, b.id, ds, de)
            windows = day_windows(day, h)
            if not all(any(o.start <= w.start and w.end <= o.end for o in offs) for w in windows):
                working = True
            if not has_free and await _free_for_barber(session, b, service_id, day, now):
                has_free = True
            if working and has_free:
                break
        out.append(DayAvailability(day=day, is_working=working, has_free_slots=has_free))
    return out


async def slot_problem(
    session: AsyncSession,
    *,
    barber_id: int,
    start_at: datetime,
    duration_min: int,
    buffer_min: int,
    now: datetime,
    ignore_lead_time: bool = False,
    ignore_booking_id: int | None = None,
    check_horizon: bool = True,
) -> str | None:
    """Internal: None if free, "taken" if an ACTIVE booking overlaps, else "unavailable"."""
    try:
        start_at = clock.ensure_aware(start_at)
    except ValueError:
        return "unavailable"
    if not clock.is_on_grid(start_at) or duration_min <= 0:
        return "unavailable"
    end = start_at + timedelta(minutes=duration_min)
    occ_end = end + timedelta(minutes=buffer_min)
    day = clock.to_local(start_at).date()
    if not ignore_lead_time:
        s = await get_shop_settings(session)
        if start_at < clock.ceil_to_grid(now + timedelta(minutes=s.min_lead_min)):
            return "unavailable"
        if check_horizon:
            today = clock.local_today(now)
            if day > today + timedelta(days=s.booking_horizon_days - 1):
                return "unavailable"
    hm = await hours_map(session, barber_id)
    hours = hm.get(day.weekday())
    windows = day_windows(day, hours)
    if not any(w.start <= start_at and end <= w.end for w in windows):
        return "unavailable"
    # same rule as compute_free_slots: the buffer may run past closing time, but not
    # into the break, time off or another booking
    if hours is not None and hours.break_start is not None and hours.break_end is not None:
        bs = clock.local_dt(day, hours.break_start)
        be = clock.local_dt(day, hours.break_end)
        if _overlap(start_at, occ_end, bs, be):
            return "unavailable"
    for o in await time_off_blocks(session, barber_id, start_at, occ_end):
        if _overlap(start_at, occ_end, o.start, o.end):
            return "unavailable"
    for b in await booking_blocks(
        session, barber_id, start_at, occ_end, ignore_booking_id=ignore_booking_id
    ):
        if _overlap(start_at, occ_end, b.start, b.end):
            return "taken"
    return None


async def is_slot_free(
    session: AsyncSession,
    *,
    barber_id: int,
    start_at: datetime,
    duration_min: int,
    buffer_min: int,
    now: datetime | None = None,
    ignore_lead_time: bool = False,
    ignore_booking_id: int | None = None,
) -> bool:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    return (
        await slot_problem(
            session,
            barber_id=barber_id,
            start_at=start_at,
            duration_min=duration_min,
            buffer_min=buffer_min,
            now=now,
            ignore_lead_time=ignore_lead_time,
            ignore_booking_id=ignore_booking_id,
        )
        is None
    )


async def list_proposal_slots(
    session: AsyncSession,
    *,
    booking_id: int,
    actor_user_id: int,
    limit: int = 8,
    now: datetime | None = None,
) -> list[datetime]:
    """Free starts on the booking's day, then the next 2 days; own cells ignored [#21]."""
    now = clock.ensure_aware(now) if now else clock.now_utc()
    booking = await load_booking(session, booking_id)
    await require_barber_or_admin(session, actor_user_id, booking.barber_id)
    barber = await session.get(Barber, booking.barber_id)
    if barber is None:
        return []
    hm = await hours_map(session, barber.id)
    s = await get_shop_settings(session)
    first_day = clock.to_local(booking.start_at).date()
    last_day = first_day + timedelta(days=2)
    out: list[datetime] = []
    for i in range(3):
        day = first_day + timedelta(days=i)
        hours = hm.get(day.weekday())
        if hours is None:
            continue
        blocks = await _blocks_for_day(session, barber.id, day, ignore_booking_id=booking.id)
        for t in compute_free_slots(
            day=day,
            hours=hours,
            blocks=blocks,
            duration_min=booking.duration_min,
            buffer_min=barber.buffer_min,
            step_min=barber.slot_step_min,
            now=now,
            min_lead_min=s.min_lead_min,
            last_bookable_day=last_day,
        ):
            if t == booking.start_at:
                continue
            out.append(t)
            if len(out) >= limit:
                return out
    return out


async def active_count_on_day(session: AsyncSession, barber_id: int, day: date) -> int:
    """Internal: number of ACTIVE bookings of the barber that local day."""
    s, e = clock.day_bounds_utc(day)
    return (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.barber_id == barber_id,
                Booking.status.in_(list(ACTIVE_STATUSES)),
                Booking.start_at >= s,
                Booking.start_at < e,
            )
        )
    ).scalar_one()
