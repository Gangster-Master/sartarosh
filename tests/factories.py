"""Test world built through the services (ARCHITECTURE.md §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import CONSENT_VERSION
from app.db.uow import write_tx
from app.services import barbers, catalog, schedule, settings, users

TZ = ZoneInfo("Asia/Tashkent")
# Monday 2026-10-26 09:00 local
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=TZ).astimezone(timezone.utc)
MONDAY = date(2026, 10, 26)
ENV_ADMIN_TG = 1000

_tg = count(5000)


def L(day: date, hhmm: str) -> datetime:
    """Local wall time on ``day`` -> aware UTC."""
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)), tzinfo=TZ).astimezone(timezone.utc)


def D(offset: int) -> date:
    return MONDAY + timedelta(days=offset)


@dataclass
class World:
    sm: async_sessionmaker[AsyncSession]
    admin_id: int
    ali_user_id: int
    ali_id: int
    svc30: int
    svc60: int
    clients: list[int] = field(default_factory=list)

    async def new_client(
        self, *, phone: str | None = None, name: str = "Client", consent: bool = True
    ) -> int:
        return await new_client(self.sm, phone=phone, name=name, consent=consent)

    async def new_barber(
        self, name: str = "Bekzod", *, auto_accept: bool = True, hours: bool = True
    ) -> tuple[int, int]:
        """Returns (barber_id, user_id)."""
        async with write_tx(self.sm) as s:
            ctx = await users.ensure_user(s, tg_id=next(_tg), tg_username=None, first_name=name)
            await users.accept_consent(s, user_id=ctx.user.id, version=CONSENT_VERSION)
            b = await barbers.create_barber(
                s, actor_user_id=self.admin_id, display_name=name, auto_accept=auto_accept, now=NOW
            )
            await barbers.request_link(s, user_id=ctx.user.id, token=b.invite_token, now=NOW)
            await barbers.approve_link(
                s, actor_user_id=self.admin_id, barber_id=b.id, user_id=ctx.user.id
            )
            if hours:
                await set_std_hours(s, self.admin_id, b.id)
        return b.id, ctx.user.id

    async def set(self, key: str, value: str) -> None:
        async with write_tx(self.sm) as s:
            await settings.update_setting(s, actor_user_id=self.admin_id, key=key, raw_value=value)


async def set_std_hours(s: AsyncSession, admin_id: int, barber_id: int) -> None:
    """Mon–Sat 10:00–20:00 with a 13:00–14:00 break; Sunday off."""
    for wd in range(6):
        await schedule.set_working_hours(
            s,
            actor_user_id=admin_id,
            barber_id=barber_id,
            weekday=wd,
            start_time=time(10),
            end_time=time(20),
            break_start=time(13),
            break_end=time(14),
            now=NOW,
        )


async def new_client(
    sm: async_sessionmaker[AsyncSession],
    *,
    phone: str | None = None,
    name: str = "Client",
    consent: bool = True,
    verified: bool = True,
) -> int:
    tg = next(_tg)
    async with write_tx(sm) as s:
        ctx = await users.ensure_user(s, tg_id=tg, tg_username=None, first_name=name)
        uid = ctx.user.id
        if consent:
            await users.accept_consent(s, user_id=uid, version=CONSENT_VERSION)
        if verified:
            await users.set_verified_contact(
                s, user_id=uid, raw_phone=phone or f"+99890{tg % 10_000_000:07d}"
            )
    return uid


async def build_world(sm: async_sessionmaker[AsyncSession]) -> World:
    async with write_tx(sm) as s:
        admin = await users.ensure_user(
            s, tg_id=ENV_ADMIN_TG, tg_username="adm", first_name="Admin"
        )
        admin_id = admin.user.id
        await users.accept_consent(s, user_id=admin_id, version=CONSENT_VERSION)
        svc30 = await catalog.create_service(
            s, actor_user_id=admin_id, name="Soch olish", duration_min=30, price_uzs=60_000
        )
        svc60 = await catalog.create_service(
            s, actor_user_id=admin_id, name="Soch + soqol", duration_min=60, price_uzs=80_000
        )
        ali_user = await users.ensure_user(s, tg_id=2000, tg_username="ali", first_name="Ali")
        await users.accept_consent(s, user_id=ali_user.user.id, version=CONSENT_VERSION)
        ali = await barbers.create_barber(
            s, actor_user_id=admin_id, display_name="Ali", auto_accept=True, now=NOW
        )
        await barbers.request_link(s, user_id=ali_user.user.id, token=ali.invite_token, now=NOW)
        await barbers.approve_link(
            s, actor_user_id=admin_id, barber_id=ali.id, user_id=ali_user.user.id
        )
        await set_std_hours(s, admin_id, ali.id)
        # most tests want instant confirmation; approval tests turn it back on
        await settings.update_setting(
            s, actor_user_id=admin_id, key="new_client_requires_approval", raw_value="false"
        )
    return World(
        sm=sm,
        admin_id=admin_id,
        ali_user_id=ali_user.user.id,
        ali_id=ali.id,
        svc30=svc30.id,
        svc60=svc60.id,
    )


async def book(
    w: World,
    client_id: int,
    start: datetime,
    *,
    barber_id: int | None | str = "ali",
    service_id: int | None | str = "30",
    now: datetime = NOW,
    use_bonus: bool = False,
    comment: str | None = None,
):  # type: ignore[no-untyped-def]
    from app.services import bookings

    bid = w.ali_id if barber_id == "ali" else barber_id
    sid = {"30": w.svc30, "60": w.svc60}.get(service_id, service_id)  # type: ignore[arg-type]
    async with write_tx(w.sm) as s:
        return await bookings.create_booking(
            s,
            client_user_id=client_id,
            barber_id=bid,  # type: ignore[arg-type]
            service_id=sid,  # type: ignore[arg-type]
            start_at=start,
            comment=comment,
            use_bonus=use_bonus,
            now=now,
        )


async def count_rows(sm: async_sessionmaker[AsyncSession], model) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    from app.db.uow import read_tx

    async with read_tx(sm) as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()
