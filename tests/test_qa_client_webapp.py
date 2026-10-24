"""QA regression (client, Mini App): booking request validation (W-04, W-11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.factories import NOW, D, L, World
from tests.webapp_harness import Api, Clock, make_api, tg_of


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest_asyncio.fixture
async def api(world: World, clk: Clock) -> AsyncIterator[Api]:
    a = await make_api(world.sm)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def test_booking_without_service_refused_when_shop_has_services(
    world: World, api: Api
) -> None:
    """The shop sells services (60 000 / 80 000 so'm). A crafted request without
    service_id books a 0 so'm "no service" slot at the barber."""
    c = await world.new_client()
    tg = await tg_of(world.sm, c)
    st, data = await api.post(
        tg,
        "/api/bookings",
        {"barber_id": world.ali_id, "start_at": iso(L(D(1), "11:00"))},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert st == 400, (st, data)
