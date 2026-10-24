"""Bot side of the Mini App: hub web_app button, menu button, chat contact fallback, and a
web booking producing the same Telegram decision message as the bot (docs/WEBAPP.md §2, §5)."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup, MenuButtonCommands, MenuButtonWebApp, WebAppInfo

from app.bot import views
from app.bot.callbacks import BookingCb
from app.bot.handlers import (
    ORDER,
    barber_manual,
    client_booking,
    fallback,
    webapp_contact,
)
from app.bot.i18n import t
from app.bot.menu_button import setup_menu_button
from app.config import Config
from app.db.uow import read_tx
from app.services import users
from app.webapp import server
from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.bot_harness import Harness
from tests.factories import MONDAY, NOW, L, World, new_client
from tests.webapp_harness import Clock, make_api, tg_of

URL = "https://shop.example.uz/"


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


def _webapp_on(monkeypatch: pytest.MonkeyPatch, *, running: bool = True) -> None:
    cfg = Config(webapp_enabled=True, webapp_url=URL)
    monkeypatch.setattr("app.config.get_config", lambda: cfg)
    monkeypatch.setattr(server, "RUNNING", running)


def _webapp_urls(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.web_app.url for row in markup.inline_keyboard for b in row if b.web_app]


def test_contact_router_order() -> None:
    idx = ORDER.index
    assert idx(barber_manual) < idx(webapp_contact)
    assert idx(client_booking) < idx(webapp_contact) < idx(fallback)


async def test_hub_button_only_when_url_and_running(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = await world.new_client()
    tg = await tg_of(world.sm, uid)
    async with read_tx(world.sm) as s:
        ctx = await users.load_context(s, tg_id=tg)
    assert ctx is not None
    _, markup = await views.hub_content(world.sm, ctx.user, ctx.roles, "uz")
    assert _webapp_urls(markup) == []  # default: disabled

    _webapp_on(monkeypatch, running=False)
    _, markup = await views.hub_content(world.sm, ctx.user, ctx.roles, "uz")
    assert _webapp_urls(markup) == []  # server failed to start

    _webapp_on(monkeypatch)
    _, markup = await views.hub_content(world.sm, ctx.user, ctx.roles, "uz")
    first = markup.inline_keyboard[0][0]
    assert first.text == "🌐 Ilovani ochish" and first.web_app == WebAppInfo(url=URL)

    monkeypatch.setattr(
        "app.config.get_config", lambda: Config(webapp_enabled=True, webapp_url="http://x")
    )
    _, markup = await views.hub_content(world.sm, ctx.user, ctx.roles, "uz")
    assert _webapp_urls(markup) == []  # not https


class _MenuBot:
    def __init__(self, current: Any) -> None:
        self.current = current
        self.set: list[Any] = []

    async def set_chat_menu_button(self, menu_button: Any) -> bool:
        self.set.append(menu_button)
        return True

    async def get_chat_menu_button(self) -> Any:
        return self.current


async def test_menu_button() -> None:
    bot = _MenuBot(MenuButtonCommands())
    await setup_menu_button(bot, URL)  # type: ignore[arg-type]
    assert isinstance(bot.set[0], MenuButtonWebApp) and bot.set[0].web_app.url == URL

    ours = _MenuBot(MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=URL)))
    await setup_menu_button(ours, None)  # type: ignore[arg-type]
    assert isinstance(ours.set[0], MenuButtonCommands)

    manual = _MenuBot(MenuButtonWebApp(text="Shop", web_app=WebAppInfo(url=URL)))
    await setup_menu_button(manual, None)  # type: ignore[arg-type]
    assert manual.set == []  # set by hand in BotFather: left alone


async def test_contact_in_chat_saves_phone(world: World, clk: Clock) -> None:
    H = Harness(world.sm)  # noqa: N806
    uid = await new_client(world.sm, verified=False)
    tg = await tg_of(world.sm, uid)
    await H.contact(tg, "+998901234567")
    async with read_tx(world.sm) as s:
        u = await users.get_user(s, uid)
    assert u.phone == "+998901234567" and u.phone_verified
    assert H.texts_to(tg)[-1] == t("uz", "contact.saved_webapp")
    n = len(H.texts_to(tg))
    await H.contact(tg, "+998901234567")  # Mini App saved it first: stay silent
    assert len(H.texts_to(tg)) == n
    await H.contact(tg, "+998901234567", owner=tg + 1)
    assert H.texts_to(tg)[-1] == t("uz", "contact.only_own")


async def test_web_booking_sends_bot_decision_message(world: World, clk: Clock) -> None:
    """Real Notifier on a fake Telegram session: the barber gets the same decision message
    with Accept/Reject buttons, and accepting it in Telegram shows up in the web app."""
    await world.set("new_client_requires_approval", "true")
    H = Harness(world.sm)  # noqa: N806
    api = await make_api(world.sm, notifier=H.notifier, bot=H.bot)
    try:
        uid = await world.new_client()
        tg = await tg_of(world.sm, uid)
        st, body = await api.post(
            tg,
            "/api/bookings",
            {
                "barber_id": world.ali_id,
                "service_id": world.svc30,
                "start_at": iso(L(MONDAY, "15:00")),
            },
            headers={"Idempotency-Key": "web-1"},
        )
        assert st == 201 and body["booking"]["status"] == "PENDING"
        bid = body["booking"]["id"]
        barber_tg = 2000
        cbs = H.last_callbacks(barber_tg)
        assert BookingCb(a="acc", id=bid).pack() in cbs
        assert BookingCb(a="rej", id=bid).pack() in cbs

        await H.tap(barber_tg, BookingCb(a="acc", id=bid).pack(), message_id=1)
        st, body = await api.get(tg, f"/api/bookings/{bid}")
        assert body["booking"]["status"] == "CONFIRMED"
        # a late tap in the web app is a no-op: no second notification
        sent = len(H.session.calls)
        st, body = await api.post(
            await tg_of(world.sm, world.ali_user_id), f"/api/barber/bookings/{bid}/accept"
        )
        await drain_pending_notifications()
        assert st == 200 and len(H.session.calls) == sent
    finally:
        await api.client.close()


async def test_server_lifecycle_and_static(world: World) -> None:
    import aiohttp

    from tests.webapp_harness import TOKEN, FakeNotifier

    app = server.create_app(
        sm=world.sm, bot=None, notifier=FakeNotifier(), bot_token=TOKEN, bot_username="b"
    )
    handle = await server.start_webapp(app, host="127.0.0.1", port=0)
    try:
        assert server.RUNNING is True
        host, port = handle.runner.addresses[0][:2]
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://{host}:{port}/healthz") as r:
                assert r.status == 200 and await r.json() == {"ok": True}
            if (server.STATIC_DIR / "index.html").is_file():
                async with http.get(f"http://{host}:{port}/") as r:
                    assert r.status == 200 and r.headers["Cache-Control"] == "no-cache"
            async with http.get(f"http://{host}:{port}/static/../../config.py") as r:
                assert r.status in (403, 404)
    finally:
        await handle.stop()
    assert server.RUNNING is False


async def test_port_in_use_raises_oserror(world: World) -> None:
    from tests.webapp_harness import TOKEN, FakeNotifier

    def mk():  # type: ignore[no-untyped-def]
        return server.create_app(
            sm=world.sm, bot=None, notifier=FakeNotifier(), bot_token=TOKEN, bot_username="b"
        )

    first = await server.start_webapp(mk(), host="127.0.0.1", port=0)
    try:
        port = first.runner.addresses[0][1]
        with pytest.raises(OSError):
            await server.start_webapp(mk(), host="127.0.0.1", port=port)
    finally:
        await first.stop()
