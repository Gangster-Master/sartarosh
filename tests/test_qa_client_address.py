"""QA regression (client): address page "open now" status (C-08)."""

from __future__ import annotations

import pytest

from app.bot.callbacks import HubCb
from app.bot.i18n import t
from app.db.uow import read_tx, write_tx
from app.services import schedule, users
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World
from tests.webapp_harness import Clock


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


async def test_shop_wide_day_off_today_is_not_shown_as_open(world: World, clk: Clock) -> None:
    async with write_tx(world.sm) as s:
        await schedule.add_time_off(
            s,
            actor_user_id=world.admin_id,
            barber_id=None,
            start_at=L(D(0), "00:00"),
            end_at=L(D(1), "00:00"),
            reason="Bayram",
            cancel_conflicts=True,
            now=NOW,
        )
    clk.now = L(D(0), "11:00")
    c = await world.new_client()
    async with read_tx(world.sm) as s:
        tg = (await users.get_user(s, c)).tg_id
    H = Harness(world.sm)  # noqa: N806
    await H.tap(tg, HubCb(a="info").pack(), message_id=7)
    page = H.texts_to(tg)[-1]
    opened = t("uz", "info.today_hours", hours="10:00–20:00", status=t("uz", "info.open_now"))
    # the whole shop is closed today (holiday), yet the page says "Bugun: 10:00–20:00 (ochiq)"
    assert opened not in page, page
