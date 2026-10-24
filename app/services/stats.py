"""Admin statistics (ARCHITECTURE.md §4.14)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BookingSource, BookingStatus
from app.db.models import Barber, Booking, User
from app.services import clock
from app.services._common import require_admin
from app.services.dto import BarberStatsRow, ServiceStatsRow, StatsDTO
from app.services.errors import InputError

_CANCELLED = {BookingStatus.CANCELLED_BY_CLIENT, BookingStatus.CANCELLED_BY_BARBER}


def _row(barber_id: int, name: str, items: list[Booking]) -> BarberStatsRow:
    done = [b for b in items if b.status == BookingStatus.COMPLETED]
    return BarberStatsRow(
        barber_id=barber_id,
        barber_name=name,
        total=len(items),
        completed=len(done),
        auto_completed=sum(1 for b in done if b.auto_completed),
        manual_completed=sum(1 for b in done if b.source == BookingSource.MANUAL),
        no_show=sum(1 for b in items if b.status == BookingStatus.NO_SHOW),
        cancelled=sum(1 for b in items if b.status in _CANCELLED),
        rejected=sum(1 for b in items if b.status == BookingStatus.REJECTED),
        expired=sum(1 for b in items if b.status == BookingStatus.EXPIRED),
        revenue_uzs=sum(b.final_price_uzs for b in done),
    )


async def get_stats(
    session: AsyncSession,
    *,
    actor_user_id: int,
    start: date,
    end: date,
    barber_id: int | None = None,
) -> StatsDTO:
    """Local-day buckets via day_bounds_utc; revenue = SUM(final_price_uzs) of COMPLETED."""
    await require_admin(session, actor_user_id)
    if end < start:
        raise InputError("end before start", field="end")
    s_utc = clock.day_bounds_utc(start)[0]
    e_utc = clock.day_bounds_utc(end)[1]
    stmt = select(Booking).where(Booking.start_at >= s_utc, Booking.start_at < e_utc)
    if barber_id is not None:
        stmt = stmt.where(Booking.barber_id == barber_id)
    items = list((await session.execute(stmt)).scalars())

    names = dict((await session.execute(select(Barber.id, Barber.display_name))).all())
    by_barber: dict[int, list[Booking]] = {}
    for b in items:
        by_barber.setdefault(b.barber_id, []).append(b)
    order = (
        (await session.execute(select(Barber.id).order_by(Barber.sort_order, Barber.id)))
        .scalars()
        .all()
    )
    rows = [_row(bid, names.get(bid, ""), by_barber[bid]) for bid in order if bid in by_barber]
    totals = _row(0, "", items)

    svc: dict[str, list[int]] = {}
    for b in items:
        if b.status != BookingStatus.COMPLETED:
            continue
        key = b.service_name_snapshot or ""
        acc = svc.setdefault(key, [0, 0])
        acc[0] += 1
        acc[1] += b.final_price_uzs
    top = sorted(
        (ServiceStatsRow(service_name=k, completed=v[0], revenue_uzs=v[1]) for k, v in svc.items()),
        key=lambda r: (-r.completed, -r.revenue_uzs, r.service_name),
    )[:5]

    new_clients = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.created_at >= s_utc, User.created_at < e_utc, User.deleted_at.is_(None))
        )
    ).scalar_one()
    return StatsDTO(
        start=start, end=end, rows=rows, totals=totals, top_services=top, new_clients=new_clients
    )
