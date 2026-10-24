"""Judge-pass fixes for the Mini App: request-phone fallback, allows_write_to_bot,
rightmost X-Forwarded-For, limiter eviction, id cap, late-accept in the barber queue,
bot sync after consent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from app.db.uow import read_tx, write_tx
from app.services import bookings
from app.webapp.ratelimit import RateLimiter
from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.factories import MONDAY, NOW, L, World
from tests.webapp_harness import Api, Clock, make_api, tg_of


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []
        self.commands: list[Any] = []

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None, **_: Any):
        self.sent.append((chat_id, text, reply_markup))
        return object()

    async def set_my_commands(self, *a: Any, **kw: Any) -> bool:
        self.commands.append((a, kw))
        return True

    async def delete_my_commands(self, *a: Any, **kw: Any) -> bool:
        self.commands.append((a, kw))
        return True


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest_asyncio.fixture
async def api(world: World, clk: Clock, bot: FakeBot) -> AsyncIterator[Api]:
    a = await make_api(world.sm, bot=bot, trust_proxy=True)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def test_request_phone_sends_contact_keyboard(world: World, api: Api, bot: FakeBot) -> None:
    uid = await world.new_client()
    tg = await tg_of(world.sm, uid)
    st, body = await api.post(tg, "/api/me/request-phone")
    assert st == 200 and body == {"sent": True}
    chat_id, text, kb = bot.sent[-1]
    assert chat_id == tg and "Raqamni yuborish" in text
    assert kb.keyboard[0][0].request_contact is True


async def test_me_exposes_allows_write_to_bot(world: World, api: Api) -> None:
    uid = await world.new_client()
    tg = await tg_of(world.sm, uid)
    st, body = await api.get(tg, "/api/me")
    assert st == 200 and body["allows_write_to_bot"] is None
    user = {"id": tg, "first_name": "Test", "allows_write_to_bot": True}
    st, body = await api.get(tg, "/api/me", user=user)
    assert body["allows_write_to_bot"] is True


async def test_consent_syncs_bot_menu(world: World, api: Api, bot: FakeBot) -> None:
    st, _ = await api.get(777001, "/api/me")
    assert st == 200
    st, _ = await api.post(777001, "/api/me/consent")
    assert st == 200
    await drain_pending_notifications()
    assert any(chat == 777001 for chat, _, _ in bot.sent)


async def test_huge_id_is_400_not_500(world: World, api: Api) -> None:
    uid = await world.new_client()
    tg = await tg_of(world.sm, uid)
    body = {"barber_id": 10**30, "service_id": world.svc30, "start_at": iso(L(MONDAY, "11:00"))}
    st, data = await api.post(tg, "/api/booking/quote", body)
    assert st == 400 and data["error"]["code"] == "invalid"


async def test_rightmost_forwarded_for_is_used(world: World, api: Api) -> None:
    from app.webapp import auth

    seen: list[str] = []
    orig = auth.client_ip

    class R:
        app = {auth.TRUST_PROXY_KEY: True}
        headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
        remote = "127.0.0.1"

    seen.append(orig(R()))  # type: ignore[arg-type]
    assert seen == ["203.0.113.9"]


def test_limiter_evicts_oldest_not_all() -> None:
    lim = RateLimiter(max_keys=3)
    assert lim.hit("a", 1, now=0)
    assert not lim.hit("a", 1, now=1)
    for k in ("b", "c", "d"):
        lim.hit(k, 5, now=1)
    # "a" (oldest) was evicted, the others keep their history
    assert lim.hit("a", 1, now=2)
    assert len(lim._hits) == 3


async def test_queue_shows_late_acceptable_expired(world: World, api: Api, clk: Clock) -> None:
    await world.set("new_client_requires_approval", "true")
    uid = await world.new_client()
    tg = await tg_of(world.sm, uid)
    body = {
        "barber_id": world.ali_id,
        "service_id": world.svc30,
        "start_at": iso(L(MONDAY, "15:00")),
    }
    st, res = await api.post(tg, "/api/bookings", body, headers={"Idempotency-Key": str(uuid4())})
    assert st == 201
    bid = res["booking"]["id"]
    async with read_tx(world.sm) as s:
        exp = (await bookings.get_booking(s, bid)).expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    clk.now = exp
    ali_tg = await tg_of(world.sm, world.ali_user_id)
    st, q = await api.get(ali_tg, f"/api/barber/queue?day={MONDAY}")
    item = next(x for x in q["bookings"] if x["id"] == bid)
    assert item["status"] == "EXPIRED" and item["actions"]["accept"] is True
    st, r = await api.post(ali_tg, f"/api/barber/bookings/{bid}/accept")
    assert st == 200 and r["booking"]["status"] == "CONFIRMED"
