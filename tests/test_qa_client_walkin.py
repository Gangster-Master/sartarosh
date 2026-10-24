"""QA regression (client): walk-in "edingizmi?" answers and client pushes for walk-ins (C-07)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.bot.callbacks import BookingCb
from app.bot.i18n import t
from app.db.enums import NotificationKind
from app.db.models import ScheduledNotification
from app.db.uow import read_tx, write_tx
from app.services import bookings, users
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World
from tests.webapp_harness import Clock


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


async def _walk_in_completed(w: World, clk: Clock) -> tuple[int, int, int]:
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
    async with read_tx(w.sm) as s:
        tg = (await users.get_user(s, c)).tg_id
    return b.id, c, tg


async def test_yes_after_no_does_not_claim_the_visit_was_confirmed(
    world: World, clk: Clock
) -> None:
    bid, c, tg = await _walk_in_completed(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, BookingCb(a="vn", id=bid).pack(), message_id=9)
    # the same question again (e.g. a second copy of the prompt); now "Ha"
    await H.tap(tg, BookingCb(a="vy", id=bid).pack(), message_id=11)
    async with read_tx(world.sm) as s:
        b = await bookings.get_booking(s, bid)
    assert b.visit_confirmed is False  # first answer wins (by design)
    # ...but the client is told "thanks, visit confirmed" + stamp progress
    assert t("uz", "my.visit_yes") not in H.texts_to(tg)[-1]


async def test_no_after_yes_does_not_claim_the_visit_was_denied(world: World, clk: Clock) -> None:
    bid, c, tg = await _walk_in_completed(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, BookingCb(a="vy", id=bid).pack(), message_id=9)
    await H.tap(tg, BookingCb(a="vn", id=bid).pack(), message_id=11)
    async with read_tx(world.sm) as s:
        b = await bookings.get_booking(s, bid)
    assert b.visit_confirmed is True
    assert t("uz", "my.visit_no") not in H.texts_to(tg)[-1]


async def test_walk_in_client_gets_only_the_visit_check_push(world: World, clk: Clock) -> None:
    """REQUIREMENTS B9: a walk-in sends no client pushes, except the visit check C19."""
    bid, c, tg = await _walk_in_completed(world, clk)
    async with read_tx(world.sm) as s:
        kinds = {
            k
            for (k,) in (
                await s.execute(
                    select(ScheduledNotification.kind).where(
                        ScheduledNotification.booking_id == bid,
                        ScheduledNotification.cancelled_at.is_(None),
                    )
                )
            ).all()
        }
    assert kinds <= {NotificationKind.MANUAL_VISIT_CONFIRM}, kinds
