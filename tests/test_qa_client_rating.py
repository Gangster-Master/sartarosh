"""QA regression (client): rating integrity now that ratings are public (D-2, D-3, C-03, S-01)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.bot.callbacks import BookingCb, RateCb
from app.bot.i18n import t
from app.db.enums import NotificationKind
from app.db.models import Review, ScheduledNotification
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, notifications, users
from tests.bot_harness import Harness
from tests.factories import ENV_ADMIN_TG, NOW, D, L, World, book
from tests.webapp_harness import Clock


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


async def _user(w: World, uid: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        return await users.get_user(s, uid)


async def _rating(w: World, barber_id: int) -> tuple[float | None, int]:
    async with read_tx(w.sm) as s:
        b = await barbers.get_barber(s, barber_id)
    return b.rating_avg, b.rating_count


async def _walk_in_completed(w: World, clk: Clock) -> tuple[int, int, int]:
    """Ali records a walk-in for a registered client's phone and marks it done."""
    c = await w.new_client(phone="+998901112233")
    async with write_tx(w.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=w.ali_user_id,
            barber_id=w.ali_id,
            service_id=w.svc30,
            start_at=L(D(0), "10:00"),
            client_name="Valijon",
            client_phone="+998901112233",
            now=NOW,
        )
        assert b.client_id == c
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=w.ali_user_id, now=L(D(0), "10:30")
        )
    clk.now = L(D(0), "11:00")
    return b.id, c, (await _user(w, c)).tg_id


async def test_denied_walk_in_cannot_be_rated(world: World, clk: Clock) -> None:
    bid, c, tg = await _walk_in_completed(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, BookingCb(a="vn", id=bid).pack(), message_id=9)  # "I was not there"
    assert t("uz", "my.visit_no") in H.texts_to(tg)[-1]
    await H.tap(tg, RateCb(id=bid, v=5).pack(), message_id=10)
    # a visit the client says never happened must not feed the public rating
    assert await _rating(world, world.ali_id) == (None, 0)


async def test_denied_walk_in_gets_no_review_prompt(world: World, clk: Clock) -> None:
    bid, c, tg = await _walk_in_completed(world, clk)
    async with write_tx(world.sm) as s:
        await bookings.confirm_manual_visit(s, booking_id=bid, client_user_id=c, yes=False)
    async with read_tx(world.sm) as s:
        send_at = (
            await s.execute(
                select(ScheduledNotification.send_at).where(
                    ScheduledNotification.booking_id == bid,
                    ScheduledNotification.kind == NotificationKind.REVIEW,
                )
            )
        ).scalar_one_or_none()
    if send_at is None:
        return
    async with write_tx(world.sm) as s:
        due = await notifications.claim_due(s, now=send_at + timedelta(minutes=1))
    kinds = [(d.kind, d.booking.id) for d in due]
    # "Rate your visit" for a visit the client denied
    assert (NotificationKind.REVIEW, bid) not in kinds


async def _auto_completed(w: World, clk: Clock) -> tuple[int, int, int]:
    c = await w.new_client()
    r = await book(w, c, L(D(0), "10:00"))
    async with write_tx(w.sm) as s:
        done = await bookings.auto_complete_due(s, now=L(D(1), "09:00"))
    assert [x.booking.id for x in done] == [r.booking.id] and done[0].booking.auto_completed
    clk.now = L(D(1), "09:05")
    return r.booking.id, c, (await _user(w, c)).tg_id


async def test_rating_removed_when_auto_completed_visit_flipped_to_no_show(
    world: World, clk: Clock
) -> None:
    bid, c, tg = await _auto_completed(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, RateCb(id=bid, v=5).pack(), message_id=9)  # crafted: no prompt was sent
    async with write_tx(world.sm) as s:
        await bookings.mark_no_show(
            s, booking_id=bid, actor_user_id=world.ali_user_id, now=L(D(1), "09:10")
        )
    # the visit never happened: its 5 stars must not stay in the public rating
    assert await _rating(world, world.ali_id) == (None, 0)


async def test_auto_completed_visit_cannot_be_rated_by_crafted_button(
    world: World, clk: Clock
) -> None:
    bid, c, tg = await _auto_completed(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, RateCb(id=bid, v=1).pack(), message_id=9)
    async with read_tx(world.sm) as s:
        assert await s.get(Review, bid) is None


async def test_low_rating_alert_sent_once_on_repeated_tap(world: World, clk: Clock) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "10:00"))
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "10:30")
        )
    clk.now = L(D(0), "13:00")
    tg = (await _user(world, c)).tg_id
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, RateCb(id=r.booking.id, v=2).pack(), message_id=9)
    # second tap from another copy of the prompt (review reminder + old chat message)
    await H.tap(tg, RateCb(id=r.booking.id, v=2).pack(), message_id=11)
    alert = t("uz", "admin.alert.low_rating", rating=2)
    sent = [x for x in H.texts_to(ENV_ADMIN_TG) if alert in x]
    assert len(sent) == 1, sent
