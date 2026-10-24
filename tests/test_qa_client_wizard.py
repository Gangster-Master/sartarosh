"""QA regression (client): leaving the booking wizard at the phone step (C-09)."""

from __future__ import annotations

import pytest
from aiogram.types import ReplyKeyboardMarkup

from app.bot.callbacks import BarberCb, DayCb, HubCb, SlotCb, SvcCb, WizCb
from app.bot.i18n import t
from app.db.uow import read_tx
from app.services import users
from tests.bot_harness import Harness
from tests.factories import NOW, D, World, new_client
from tests.webapp_harness import Clock


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


def _last_reply_kb(H: Harness, tg: int) -> ReplyKeyboardMarkup | None:  # noqa: N803
    for c in reversed(H.session.calls):
        mk = getattr(c, "reply_markup", None)
        if getattr(c, "chat_id", None) == tg and isinstance(mk, ReplyKeyboardMarkup):
            return mk
    return None


async def _to_contact_step(w: World, H: Harness) -> int:  # noqa: N803
    c = await new_client(w.sm, verified=False)
    async with read_tx(w.sm) as s:
        tg = (await users.get_user(s, c)).tg_id
    await H.text(tg, t("uz", "menu.book"))
    await H.tap(tg, SvcCb(s=w.svc30).pack())
    await H.tap(tg, BarberCb(b=w.ali_id).pack())
    await H.tap(tg, DayCb(d=D(1).strftime("%Y%m%d")).pack())
    await H.tap(tg, SlotCb(b=w.ali_id, s=w.svc30, d=D(1).strftime("%Y%m%d"), t="1100").pack())
    await H.tap(tg, WizCb(a="skip_comment").pack())
    assert await H.fsm_state(tg) == "BookingFlow:contact"
    mk = _last_reply_kb(H, tg)
    assert mk is not None and mk.keyboard[0][0].request_contact
    return tg


@pytest.mark.parametrize("how", ["/my", "hub_prices"])
async def test_exit_from_phone_step_restores_menu(world: World, clk: Clock, how: str) -> None:
    H = Harness(world.sm)  # noqa: N806
    tg = await _to_contact_step(world, H)
    if how == "hub_prices":  # an inline button on an older hub message
        await H.tap(tg, HubCb(a="prices").pack(), message_id=3)
    else:
        await H.text(tg, how)
    assert await H.fsm_state(tg) is None
    mk = _last_reply_kb(H, tg)
    # the client is no longer booking, but the only reply keyboard left is "📱 send phone"
    assert mk is not None and not mk.keyboard[0][0].request_contact, mk
