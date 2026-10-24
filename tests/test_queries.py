from __future__ import annotations

from datetime import timedelta

from aiogram.fsm.storage.base import StorageKey

from app.bot.fsm_storage import DbStorage
from app.bot.states import BookingFlow
from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, catalog, fsm_store
from tests.factories import NOW, D, L, World, book


async def test_list_bookings_filters_and_paging(world: World) -> None:
    await world.set("max_active_bookings", "20")
    await world.set("max_requests_per_day", "50")
    c = await world.new_client(phone="+998901234567", name="Zafar")
    for i in range(12):
        await book(
            world,
            c,
            L(
                D(1),
                f"{10 + i // 2 if 10 + i // 2 < 13 else 11 + i // 2}:{'00' if i % 2 == 0 else '30'}",
            ),
        )
    other = await world.new_client(name="Bobur")
    await book(world, other, L(D(2), "10:00"))
    async with read_tx(world.sm) as s:
        p0 = await bookings.list_bookings(s, actor_user_id=world.admin_id, day=D(1))
        p1 = await bookings.list_bookings(s, actor_user_id=world.admin_id, day=D(1), page=1)
        by_phone = await bookings.list_bookings(
            s, actor_user_id=world.admin_id, phone_or_name="4567"
        )
        by_name = await bookings.list_bookings(s, actor_user_id=world.admin_id, phone_or_name="bob")
        by_status = await bookings.list_bookings(
            s, actor_user_id=world.admin_id, status=BookingStatus.CONFIRMED, barber_id=world.ali_id
        )
        far = await bookings.list_bookings(s, actor_user_id=world.admin_id, page=99)
    assert (p0.total, p0.pages, len(p0.items), len(p1.items)) == (12, 2, 10, 2)
    assert p0.items[0].start_at < p0.items[-1].start_at
    assert by_phone.total == 12 and by_name.total == 1 and by_status.total == 13
    assert far.page == 1


async def test_list_client_bookings_and_rebook(world: World) -> None:
    c = await world.new_client()
    done = await book(world, c, L(D(0), "10:00"), service_id="60")
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=done.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "11:00")
        )
    up = await book(world, c, L(D(2), "10:00"), now=L(D(0), "11:00"))
    async with read_tx(world.sm) as s:
        res = await bookings.list_client_bookings(s, client_user_id=c, now=L(D(0), "11:00"))
        hint = await bookings.get_last_completed(s, client_user_id=c)
    assert [b.id for b in res.upcoming] == [up.booking.id]
    assert [b.id for b in res.past] == [done.booking.id]
    assert hint is not None and hint.barber_id == world.ali_id and hint.service_id == world.svc60
    async with write_tx(world.sm) as s:
        await catalog.update_service(
            s, actor_user_id=world.admin_id, service_id=world.svc60, is_active=False
        )
    async with read_tx(world.sm) as s:
        hint = await bookings.get_last_completed(s, client_user_id=c)
    assert hint is not None and hint.service_id is None


async def test_pending_digest_and_daily_summary(world: World) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    r = await book(world, c, L(D(0), "10:00"), now=L(D(-1), "23:00"))
    async with read_tx(world.sm) as s:
        digest = await bookings.list_pending_digest(s, day=D(0), now=L(D(0), "08:00"))
        summary = await bookings.bookings_for_daily_summary(s, day=D(0))
    assert [i.booking.id for i in digest] == [r.booking.id]
    assert digest[0].targets
    assert [b.id for b in summary[world.ali_id]] == [r.booking.id]


async def test_decision_messages_idempotent(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.add_decision_message(
            s, booking_id=r.booking.id, chat_id=1, message_id=2, role="barber"
        )
        await bookings.add_decision_message(
            s, booking_id=r.booking.id, chat_id=1, message_id=2, role="barber"
        )
        await bookings.add_decision_message(
            s, booking_id=r.booking.id, chat_id=1000, message_id=3, role="admin"
        )
    async with read_tx(world.sm) as s:
        assert await bookings.list_decision_messages(s, r.booking.id) == [
            (1, 2, "barber"),
            (1000, 3, "admin"),
        ]


async def test_db_fsm_storage_survives_restart(sm) -> None:  # type: ignore[no-untyped-def]
    key = StorageKey(bot_id=42, chat_id=7, user_id=7)
    st = DbStorage(sm)
    await st.set_state(key, BookingFlow.slot)
    await st.set_data(key, {"day": "2026-10-27", "nav": ["service"], "when": NOW})
    fresh = DbStorage(sm)  # "restart": empty cache
    assert await fresh.get_state(key) == BookingFlow.slot.state
    data = await fresh.get_data(key)
    assert data["day"] == "2026-10-27" and data["nav"] == ["service"]
    data["nav"].append("x")  # callers can't mutate the cache
    assert (await fresh.get_data(key))["nav"] == ["service"]
    await fresh.set_state(key, None)
    assert await DbStorage(sm).get_state(key) is None
    async with write_tx(sm) as s:
        assert await fsm_store.purge_older_than(s, days=7, now=NOW + timedelta(days=400)) == 1
