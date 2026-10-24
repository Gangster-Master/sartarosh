"""QA regression (client): a barber whose working hours were all removed is hidden from the
lists, but other client entry points still treat him as bookable (D-1, C-01, C-02, W-04)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.bot.callbacks import BarberCardCb, HubCb, RebookCb
from app.bot.i18n import t
from app.db.uow import read_tx, write_tx
from app.services import bookings, schedule, users
from app.webapp.tasks import drain_pending_notifications
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World, book
from tests.webapp_harness import Api, Clock, make_api, tg_of


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def _visit_then_drop_hours(w: World, clk: Clock) -> tuple[int, int, int]:
    """Client completes a visit with Bekzod; then the admin removes all Bekzod's hours."""
    bek, bek_user = await w.new_barber("Bekzod")
    c = await w.new_client()
    r = await book(w, c, L(D(0), "10:00"), barber_id=bek)
    async with write_tx(w.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=bek_user, now=L(D(0), "10:40")
        )
        for wd in range(7):
            await schedule.set_working_hours(
                s,
                actor_user_id=w.admin_id,
                barber_id=bek,
                weekday=wd,
                start_time=None,
                end_time=None,
                now=L(D(0), "11:00"),
            )
    clk.now = L(D(0), "12:00")
    return bek, c, await _tg(w, c)


async def test_hub_rebook_to_hourless_barber_is_not_a_dead_end(world: World, clk: Clock) -> None:
    bek, c, tg = await _visit_then_drop_hours(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, HubCb(a="rebook").pack(), message_id=7)
    # expected: the barber is not bookable -> the wizard must not open his (empty) day step
    assert await H.fsm_state(tg) != "BookingFlow:day"
    assert t("uz", "book.no_days") not in H.texts_to(tg)[-1]


async def test_rebook_button_to_hourless_barber_is_not_a_dead_end(world: World, clk: Clock) -> None:
    bek, c, tg = await _visit_then_drop_hours(world, clk)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, RebookCb(b=bek, s=world.svc30).pack(), message_id=7)
    assert await H.fsm_state(tg) != "BookingFlow:day"
    assert t("uz", "book.no_days") not in H.texts_to(tg)[-1]


async def test_barber_card_of_hourless_barber_offers_no_booking(world: World, clk: Clock) -> None:
    bek, c, tg = await _visit_then_drop_hours(world, clk)
    H = Harness(world.sm)  # noqa: N806
    # an old prices message still carries the card button
    await H.tap(tg, BarberCardCb(a="view", b=bek).pack(), message_id=7)
    assert BarberCardCb(a="book", b=bek).pack() not in H.last_callbacks(tg)
    assert t("uz", "info.barber_unavailable") in H.texts_to(tg)[-1]
    # and tapping an old "book this" button must explain instead of opening an empty wizard
    await H.tap(tg, BarberCardCb(a="book", b=bek).pack(), message_id=7)
    assert await H.fsm_state(tg) != "BookingFlow:day"


@pytest_asyncio.fixture
async def api(world: World, clk: Clock) -> AsyncIterator[Api]:
    a = await make_api(world.sm)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def test_webapp_home_rebook_hint_skips_hourless_barber(
    world: World, clk: Clock, api: Api
) -> None:
    bek, c, tg = await _visit_then_drop_hours(world, clk)
    st, data = await api.get(await tg_of(world.sm, c), "/api/home")
    assert st == 200
    # the "🔁 Avvalgidek" card would lead to a barber with no days at all
    assert data["rebook"] is None or data["rebook"]["barber_id"] != bek
