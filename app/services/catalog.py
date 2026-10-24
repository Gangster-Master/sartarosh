"""Services and barber_services (ARCHITECTURE.md §4.8)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Barber, BarberService, Service
from app.services import clock
from app.services._common import barber_dtos, load_barber, require_admin
from app.services.audit import write_audit
from app.services.dto import BarberDTO, ResolvedService, ServiceDTO
from app.services.errors import InputError, NotFoundError


def _svc_dto(s: Service) -> ServiceDTO:
    return ServiceDTO(
        id=s.id,
        name=s.name,
        duration_min=s.duration_min,
        price_uzs=s.price_uzs,
        is_active=bool(s.is_active),
        sort_order=s.sort_order or 0,
    )


def _check_duration(v: int, field: str = "duration_min") -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise InputError("invalid duration", field=field) from None
    if v <= 0 or v % clock.SLOT_UNIT_MIN != 0 or v > 240:
        raise InputError("invalid duration", field=field)
    return v


def _check_price(v: int, field: str = "price_uzs") -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise InputError("invalid price", field=field) from None
    if v < 0:
        raise InputError("invalid price", field=field)
    return v


def _check_name(v: str) -> str:
    name = " ".join((v or "").split())
    if not 1 <= len(name) <= 64:
        raise InputError("invalid name", field="name")
    return name


async def _offered(session: AsyncSession, barber_id: int) -> list[ResolvedService]:
    """Active services the barber offers (with per-barber overrides), ordered."""
    rows = (
        (await session.execute(select(BarberService).where(BarberService.barber_id == barber_id)))
        .scalars()
        .all()
    )
    overrides = {r.service_id: r for r in rows}
    stmt = select(Service).where(Service.is_active.is_(True))
    if rows:
        stmt = stmt.where(Service.id.in_(list(overrides)))
    services = (
        (await session.execute(stmt.order_by(Service.sort_order, Service.id))).scalars().all()
    )
    out = []
    for s in services:
        ov = overrides.get(s.id)
        out.append(
            ResolvedService(
                service_id=s.id,
                name=s.name,
                duration_min=(ov.duration_min if ov and ov.duration_min else s.duration_min),
                price_uzs=(ov.price_uzs if ov and ov.price_uzs is not None else s.price_uzs),
            )
        )
    return out


async def list_services(session: AsyncSession, *, active_only: bool = True) -> list[ServiceDTO]:
    stmt = select(Service)
    if active_only:
        stmt = stmt.where(Service.is_active.is_(True))
    rows = (await session.execute(stmt.order_by(Service.sort_order, Service.id))).scalars().all()
    return [_svc_dto(s) for s in rows]


async def list_barber_services(session: AsyncSession, barber_id: int) -> list[ResolvedService]:
    await load_barber(session, barber_id)
    return await _offered(session, barber_id)


async def _public_barbers(session: AsyncSession) -> list[Barber]:
    return list(
        (
            await session.execute(
                select(Barber)
                .where(Barber.is_active.is_(True), Barber.user_id.is_not(None))
                .order_by(Barber.sort_order, Barber.id)
            )
        ).scalars()
    )


async def list_bookable_services(session: AsyncSession) -> list[ServiceDTO]:
    """Active services offered by at least one public barber [#1]."""
    ids: set[int] = set()
    for b in await _public_barbers(session):
        ids.update(rs.service_id for rs in await _offered(session, b.id) if rs.service_id)
    if not ids:
        return []
    rows = (
        (
            await session.execute(
                select(Service).where(Service.id.in_(ids)).order_by(Service.sort_order, Service.id)
            )
        )
        .scalars()
        .all()
    )
    return [_svc_dto(s) for s in rows]


async def _barbers_for_service_rows(session: AsyncSession, service_id: int | None) -> list[Barber]:
    barbers = await _public_barbers(session)
    if service_id is None:
        return barbers
    out = []
    for b in barbers:
        if any(rs.service_id == service_id for rs in await _offered(session, b.id)):
            out.append(b)
    return out


async def list_barbers_for_service(
    session: AsyncSession, *, service_id: int | None
) -> list[BarberDTO]:
    return await barber_dtos(session, await _barbers_for_service_rows(session, service_id))


async def resolve_service(
    session: AsyncSession, *, barber_id: int, service_id: int | None
) -> ResolvedService:
    barber = await load_barber(session, barber_id)
    if service_id is None:
        return ResolvedService(
            service_id=None, name=None, duration_min=barber.slot_step_min, price_uzs=0
        )
    for rs in await _offered(session, barber_id):
        if rs.service_id == service_id:
            return rs
    raise InputError("service not offered", field="service")


async def create_service(
    session: AsyncSession, *, actor_user_id: int, name: str, duration_min: int, price_uzs: int
) -> ServiceDTO:
    await require_admin(session, actor_user_id)
    now = clock.now_utc()
    max_sort = (await session.execute(select(func.max(Service.sort_order)))).scalar() or 0
    s = Service(
        name=_check_name(name),
        duration_min=_check_duration(duration_min),
        price_uzs=_check_price(price_uzs),
        is_active=True,
        sort_order=max_sort + 1,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="service.create",
        entity="service",
        entity_id=s.id,
        payload={"name": s.name, "duration_min": s.duration_min, "price_uzs": s.price_uzs},
    )
    return _svc_dto(s)


async def update_service(
    session: AsyncSession,
    *,
    actor_user_id: int,
    service_id: int,
    name: str | None = None,
    duration_min: int | None = None,
    price_uzs: int | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> ServiceDTO:
    await require_admin(session, actor_user_id)
    s = await session.get(Service, service_id)
    if s is None:
        raise NotFoundError("service not found", entity="service", id=service_id)
    changes: dict[str, object] = {}
    if name is not None:
        s.name = changes["name"] = _check_name(name)
    if duration_min is not None:
        s.duration_min = changes["duration_min"] = _check_duration(duration_min)
    if price_uzs is not None:
        s.price_uzs = changes["price_uzs"] = _check_price(price_uzs)
    if is_active is not None:
        s.is_active = changes["is_active"] = bool(is_active)
    if sort_order is not None:
        s.sort_order = changes["sort_order"] = int(sort_order)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="service.update",
        entity="service",
        entity_id=s.id,
        payload=changes,
    )
    return _svc_dto(s)


async def set_barber_services(
    session: AsyncSession, *, actor_user_id: int, barber_id: int, service_ids: list[int]
) -> list[ResolvedService]:
    """Replace the barber's offer. An empty list means "all active services"."""
    await require_admin(session, actor_user_id)
    await load_barber(session, barber_id)
    wanted = set(int(i) for i in service_ids)
    if wanted:
        found = set(
            (await session.execute(select(Service.id).where(Service.id.in_(wanted)))).scalars()
        )
        missing = wanted - found
        if missing:
            raise InputError("unknown service", field="service", ids=sorted(missing))
    rows = (
        (await session.execute(select(BarberService).where(BarberService.barber_id == barber_id)))
        .scalars()
        .all()
    )
    existing = {r.service_id: r for r in rows}
    for sid, row in existing.items():
        if sid not in wanted:
            await session.delete(row)
    for sid in wanted - set(existing):
        session.add(BarberService(barber_id=barber_id, service_id=sid))
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.services",
        entity="barber",
        entity_id=barber_id,
        payload={"service_ids": sorted(wanted)},
    )
    return await _offered(session, barber_id)


async def set_barber_service_override(
    session: AsyncSession,
    *,
    actor_user_id: int,
    barber_id: int,
    service_id: int,
    price_uzs: int | None,
    duration_min: int | None,
) -> ResolvedService:
    await require_admin(session, actor_user_id)
    await load_barber(session, barber_id)
    s = await session.get(Service, service_id)
    if s is None:
        raise NotFoundError("service not found", entity="service", id=service_id)
    if price_uzs is not None:
        price_uzs = _check_price(price_uzs)
    if duration_min is not None:
        duration_min = _check_duration(duration_min)
    rows = (
        (await session.execute(select(BarberService).where(BarberService.barber_id == barber_id)))
        .scalars()
        .all()
    )
    if not rows:
        # "no rows" means "all active services": materialise that set first so an
        # override does not silently restrict the barber to one service.
        for sid in (
            await session.execute(select(Service.id).where(Service.is_active.is_(True)))
        ).scalars():
            session.add(BarberService(barber_id=barber_id, service_id=sid))
        await session.flush()
    row = await session.get(BarberService, (barber_id, service_id))
    if row is None:
        row = BarberService(barber_id=barber_id, service_id=service_id)
        session.add(row)
    row.price_uzs = price_uzs
    row.duration_min = duration_min
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="barber.service_override",
        entity="barber",
        entity_id=barber_id,
        payload={"service_id": service_id, "price_uzs": price_uzs, "duration_min": duration_min},
    )
    return await resolve_service(session, barber_id=barber_id, service_id=service_id)


async def shop_has_services(session: AsyncSession) -> bool:
    return (
        await session.execute(select(Service.id).where(Service.is_active.is_(True)).limit(1))
    ).scalar_one_or_none() is not None
