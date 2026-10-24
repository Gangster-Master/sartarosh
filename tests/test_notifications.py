from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.enums import NotificationKind as K
from app.db.models import ScheduledNotification
from app.db.uow import read_tx, write_tx
from app.services import bookings, notifications
from tests.factories import NOW, D, L, World, book


class Row:
    def __init__(self, r: ScheduledNotification) -> None:
        self.send_at, self.claimed_at = r.send_at, r.claimed_at
        self.cancelled_at, self.sent_at, self.attempts = r.cancelled_at, r.sent_at, r.attempts


async def _rows(w: World, bid: int) -> dict:  # type: ignore[type-arg]
    async with read_tx(w.sm) as s:
        rows = (
            (
                await s.execute(
                    select(ScheduledNotification).where(ScheduledNotification.booking_id == bid)
                )
            )
            .scalars()
            .all()
        )
        return {r.kind: Row(r) for r in rows}


async def _claim(w: World, now):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await notifications.claim_due(s, now=now)


async def test_confirm_creates_r24_r2(world: World) -> None:
    c = await world.new_client()
    start = L(D(1), "10:00")  # 25 h ahead
    r = await book(world, c, start)
    rows = await _rows(world, r.booking.id)
    assert rows[K.REMINDER_24H].send_at == start - timedelta(hours=24)
    assert rows[K.REMINDER_2H].send_at == start - timedelta(hours=2)


async def test_under_24h_no_r24(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "12:00"))  # 3 h ahead
    rows = await _rows(world, r.booking.id)
    assert K.REMINDER_24H not in rows and K.REMINDER_2H in rows
    c2 = await world.new_client()
    r2 = await book(world, c2, L(D(0), "10:30"))  # 1.5 h ahead
    assert await _rows(world, r2.booking.id) == {}


async def test_cancel_cancels_rows(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.cancel_by_client(s, booking_id=r.booking.id, client_user_id=c, now=NOW)
    rows = await _rows(world, r.booking.id)
    assert all(x.cancelled_at is not None for x in rows.values())
    assert await _claim(world, L(D(1), "09:00")) == []


async def test_claim_once(world: World) -> None:
    c = await world.new_client()
    start = L(D(1), "10:00")
    r = await book(world, c, start)
    due = await _claim(world, start - timedelta(hours=24))
    assert [(n.kind, n.booking.id) for n in due] == [(K.REMINDER_24H, r.booking.id)]
    assert due[0].targets == []
    assert await _claim(world, start - timedelta(hours=23)) == []
    rows = await _rows(world, r.booking.id)
    assert rows[K.REMINDER_24H].claimed_at is not None
    async with write_tx(world.sm) as s:
        await notifications.mark_failed(s, notification_id=due[0].id, error="boom")
    assert await _claim(world, start - timedelta(hours=23)) == []  # at most once
    rows = await _rows(world, r.booking.id)
    assert rows[K.REMINDER_24H].attempts == 1


async def test_r2_skipped_after_ack(world: World) -> None:
    c = await world.new_client()
    start = L(D(1), "10:00")
    r = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.client_ack(s, booking_id=r.booking.id, client_user_id=c, now=NOW)
    due = await _claim(world, start - timedelta(hours=2))
    kinds = [n.kind for n in due]
    assert K.REMINDER_2H not in kinds
    rows = await _rows(world, r.booking.id)
    assert rows[K.REMINDER_2H].cancelled_at is not None


async def test_stale_cutoffs_restart_catch_up(world: World) -> None:
    c = await world.new_client()
    start = L(D(0), "15:00")
    r = await book(world, c, start)  # r2 at 13:00
    # bot back 10 minutes after the send time: still sent once
    due = await _claim(world, L(D(0), "13:10"))
    assert [n.kind for n in due] == [K.REMINDER_2H]
    c2 = await world.new_client()
    r2 = await book(world, c2, L(D(0), "16:00"))  # r2 at 14:00
    # stopped for longer than 30 minutes past send time: dropped, not sent late
    assert await _claim(world, L(D(0), "14:31")) == []
    assert (await _rows(world, r2.booking.id))[K.REMINDER_2H].cancelled_at is not None
    assert r


async def test_r24_stale_after_3h(world: World) -> None:
    c = await world.new_client()
    start = L(D(1), "10:00")
    r = await book(world, c, start)
    send = start - timedelta(hours=24)
    assert await _claim(world, send + timedelta(hours=3, minutes=1)) == []
    assert (await _rows(world, r.booking.id))[K.REMINDER_24H].cancelled_at is not None


async def test_rping_scheduled_and_claimed_with_targets(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    created = L(D(0), "11:00")
    r = await book(world, c, L(D(0), "15:00"), now=created)
    rows = await _rows(world, r.booking.id)
    assert rows[K.PENDING_REPING].send_at == created + timedelta(minutes=15)
    due = await _claim(world, created + timedelta(minutes=15))
    assert [n.kind for n in due] == [K.PENDING_REPING]
    assert {t.role for t in due[0].targets} == {"barber", "admin"}


async def test_rping_not_scheduled_outside_hours(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(0), "12:00"), now=L(D(-1), "23:00"))
    assert K.PENDING_REPING not in await _rows(world, r.booking.id)


async def test_rping_cancelled_by_confirm(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    created = L(D(0), "11:00")
    r = await book(world, c, L(D(0), "15:00"), now=created)
    async with write_tx(world.sm) as s:
        await bookings.confirm_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=world.ali_user_id,
            now=created + timedelta(minutes=5),
        )
    rows = await _rows(world, r.booking.id)
    assert rows[K.PENDING_REPING].cancelled_at is not None
    assert K.REMINDER_2H in rows


async def _complete(w: World, c: int, start):  # type: ignore[no-untyped-def]
    r = await book(w, c, start, now=start - timedelta(hours=1))
    async with write_tx(w.sm) as s:
        await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=w.ali_user_id,
            now=start + timedelta(minutes=30),
        )
    return r.booking.id


async def test_completion_schedules_ret_and_rev(world: World) -> None:
    c = await world.new_client()
    start = L(D(0), "18:00")
    bid = await _complete(world, c, start)
    rows = await _rows(world, bid)
    # end 18:30 + 2h = 20:30 local -> within daytime
    assert rows[K.REVIEW].send_at == L(D(0), "20:30")
    assert rows[K.RETURN].send_at == L(D(28), "18:00")


async def test_ret_skipped_if_client_rebooked(world: World) -> None:
    c = await world.new_client()
    bid = await _complete(world, c, L(D(0), "10:00"))
    send = (await _rows(world, bid))[K.RETURN].send_at
    # client has an upcoming booking at that time
    await book(world, c, send + timedelta(days=1), now=send - timedelta(hours=2))
    due = await _claim(world, send)
    assert K.RETURN not in [n.kind for n in due]
    assert (await _rows(world, bid))[K.RETURN].cancelled_at is not None


async def test_ret_sent_when_not_rebooked(world: World) -> None:
    c = await world.new_client()
    bid = await _complete(world, c, L(D(0), "10:00"))
    send = (await _rows(world, bid))[K.RETURN].send_at
    due = await _claim(world, send + timedelta(hours=1))
    assert (K.RETURN, bid) in [(n.kind, n.booking.id) for n in due]


async def test_auto_completed_has_no_review(world: World) -> None:
    c = await world.new_client()
    start = L(D(0), "10:00")
    r = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.auto_complete_due(s, now=start + timedelta(hours=14))
    rows = await _rows(world, r.booking.id)
    assert K.REVIEW not in rows and K.RETURN in rows


async def test_deleted_client_gets_nothing(world: World) -> None:
    from app.services import users

    c = await world.new_client()
    bid = await _complete(world, c, L(D(0), "10:00"))
    async with write_tx(world.sm) as s:
        await users.delete_me(s, user_id=c, now=L(D(0), "12:00"))
    due = await _claim(world, L(D(40), "12:00"))
    assert bid not in [n.booking.id for n in due]
