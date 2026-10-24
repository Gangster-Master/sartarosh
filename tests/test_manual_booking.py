from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.enums import BookingSource, BookingStatus, NotificationKind
from app.db.models import AuditLog, ScheduledNotification
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, notifications
from app.services.errors import PermissionDeniedError, SlotTakenError, SlotUnavailableError
from tests.factories import NOW, D, L, World, book, new_client


async def _manual(w: World, start, phone: str | None, *, actor=None, now=NOW):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await bookings.create_manual_booking(
            s,
            actor_user_id=actor or w.ali_user_id,
            barber_id=w.ali_id,
            service_id=w.svc30,
            start_at=start,
            client_name="Walk In",
            client_phone=phone,
            now=now,
        )


async def test_links_single_verified_match(world: World) -> None:
    c = await world.new_client(phone="+998901234567")
    b = await _manual(world, L(D(0), "10:00"), "90 123 45 67")
    assert b.source == BookingSource.MANUAL and b.status == BookingStatus.CONFIRMED
    assert b.client_id == c and b.client_phone == "+998901234567"


async def test_unverified_or_ambiguous_match_unlinked(world: World) -> None:
    await new_client(world.sm, verified=False)
    b = await _manual(world, L(D(0), "10:00"), "+998905550000")
    assert b.client_id is None
    await world.new_client(phone="+998906660000")
    await world.new_client(phone="+998906660000")
    b2 = await _manual(world, L(D(0), "11:00"), "+998906660000")
    assert b2.client_id is None


async def test_lead_time_ignored_but_not_past(world: World) -> None:
    now = L(D(0), "10:10")
    b = await _manual(world, L(D(0), "10:00"), None, now=now)  # floor(now)-15 = 09:55
    assert b.status == BookingStatus.CONFIRMED
    with pytest.raises(SlotUnavailableError):
        await _manual(world, L(D(0), "09:45"), None, now=now)
    with pytest.raises(SlotTakenError):
        await _manual(world, L(D(0), "10:00"), None, now=now)


async def test_only_own_barber_or_admin(world: World) -> None:
    _, bek_user = await world.new_barber("Bekzod")
    with pytest.raises(PermissionDeniedError):
        await _manual(world, L(D(0), "10:00"), None, actor=bek_user)
    b = await _manual(world, L(D(0), "10:00"), None, actor=world.admin_id)
    assert b.status == BookingStatus.CONFIRMED


async def test_no_reminders_for_manual(world: World) -> None:
    await world.new_client(phone="+998901234567")
    b = await _manual(world, L(D(3), "10:00"), "+998901234567")
    async with read_tx(world.sm) as s:
        rows = (
            (
                await s.execute(
                    select(ScheduledNotification).where(ScheduledNotification.booking_id == b.id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_completion_waits_for_yes(world: World) -> None:
    c = await world.new_client(phone="+998901234567")
    start = L(D(0), "10:00")
    b = await _manual(world, start, "+998901234567")
    async with write_tx(world.sm) as s:
        r = await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=world.ali_user_id, now=start + timedelta(minutes=30)
        )
    assert r.needs_visit_confirm and r.earned == 0
    async with write_tx(world.sm) as s:
        due = await notifications.claim_due(s, now=start + timedelta(minutes=31))
    assert [n.kind for n in due] == [NotificationKind.MANUAL_VISIT_CONFIRM]
    async with write_tx(world.sm) as s:
        y = await bookings.confirm_manual_visit(
            s, booking_id=b.id, client_user_id=c, yes=True, now=start + timedelta(hours=1)
        )
    assert y.earned == 1 and y.welcome_credited == 1
    async with write_tx(world.sm) as s:  # idempotent
        y2 = await bookings.confirm_manual_visit(
            s, booking_id=b.id, client_user_id=c, yes=True, now=start + timedelta(hours=1)
        )
    assert y2.earned == 0
    async with read_tx(world.sm) as s:
        assert (await bonus.get_bonus_status(s, c)).balance == 2
        acts = (await s.execute(select(AuditLog.action))).scalars().all()
    assert "booking.manual_create" in acts and "booking.manual_complete" in acts


async def test_no_writes_audit(world: World) -> None:
    c = await world.new_client(phone="+998901234567")
    start = L(D(0), "10:00")
    b = await _manual(world, start, "+998901234567")
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=world.ali_user_id, now=start
        )
    async with write_tx(world.sm) as s:
        r = await bookings.confirm_manual_visit(
            s, booking_id=b.id, client_user_id=c, yes=False, now=start
        )
    assert r.earned == 0 and r.booking.visit_confirmed is False
    async with read_tx(world.sm) as s:
        acts = (await s.execute(select(AuditLog.action))).scalars().all()
        assert (await bonus.get_bonus_status(s, c)).balance == 0
    assert "manual_visit_denied" in acts


async def test_manual_counts_toward_bot_overlap(world: World) -> None:
    c = await world.new_client(phone="+998901234567")
    await _manual(world, L(D(1), "10:00"), "+998901234567")
    from app.services.errors import ClientLimitError

    bek, _ = await world.new_barber("Bekzod")
    with pytest.raises(ClientLimitError):
        await book(world, c, L(D(1), "10:00"), barber_id=bek)


async def test_auto_completed_manual_asks_next_morning(world: World) -> None:
    await world.new_client(phone="+998901234567")
    start = L(D(0), "15:00")
    b = await _manual(world, start, "+998901234567")
    async with write_tx(world.sm) as s:
        done = await bookings.auto_complete_due(s, now=L(D(1), "03:30"))
    assert done[0].needs_visit_confirm
    async with read_tx(world.sm) as s:
        row = (
            await s.execute(
                select(ScheduledNotification).where(
                    ScheduledNotification.booking_id == b.id,
                    ScheduledNotification.kind == NotificationKind.MANUAL_VISIT_CONFIRM,
                )
            )
        ).scalar_one()
        assert row.send_at == L(D(1), "10:00")  # not at 03:30 at night
