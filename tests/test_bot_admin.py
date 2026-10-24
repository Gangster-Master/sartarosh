"""Admin flows through the bot (env admin tg 1000)."""

from __future__ import annotations

from app.bot.callbacks import AdminCb
from app.bot.i18n import t
from app.db.enums import BookingStatus
from app.db.uow import read_tx
from app.services import bookings, catalog, settings, users
from tests.bot_harness import Harness
from tests.factories import D, L, World, book

ADMIN = 1000


async def test_create_service_rejects_bad_duration_and_price(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, AdminCb(s="sv", a="new").pack(), message_id=5)
    await H.text(ADMIN, "Soqol")
    await H.text(ADMIN, "20")
    assert t("uz", "err.invalid.duration_min") in H.texts_to(ADMIN)
    await H.text(ADMIN, "15")
    await H.text(ADMIN, "-5")
    assert t("uz", "err.invalid.price_uzs") in H.texts_to(ADMIN)
    await H.text(ADMIN, "30 000 so'm")
    async with read_tx(world.sm) as s:
        svc = [x for x in await catalog.list_services(s) if x.name == "Soqol"]
    assert len(svc) == 1 and svc[0].duration_min == 15 and svc[0].price_uzs == 30_000


async def test_setting_abc_refused_then_saved(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, AdminCb(s="st", a="key", v="booking_horizon_days").pack(), message_id=5)
    await H.text(ADMIN, "abc")
    async with read_tx(world.sm) as s:
        assert (await settings.get_shop_settings(s)).booking_horizon_days == 7
    await H.text(ADMIN, "14")
    async with read_tx(world.sm) as s:
        assert (await settings.get_shop_settings(s)).booking_horizon_days == 14


async def test_client_search_block_confirm_is_not_a_toggle(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    c = await world.new_client(phone="+998901234567")
    r = await book(world, c, L(D(1), "10:00"))
    await H.tap(ADMIN, AdminCb(s="cl", a="search").pack(), message_id=5)
    await H.text(ADMIN, "45")  # fewer than 4 digits
    await H.text(ADMIN, "4567")
    blk = AdminCb(s="cl", a="blk", id=c).pack()
    assert blk in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, blk, message_id=5)
    yes = AdminCb(s="cl", a="blk", id=c, v="on").pack()
    assert yes in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, yes, message_id=5)
    await H.tap(ADMIN, yes, message_id=6)  # a stale second "yes" must not unblock
    async with read_tx(world.sm) as s:
        assert (await users.get_user(s, c)).is_blocked
        assert (
            await bookings.get_booking(s, r.booking.id)
        ).status == BookingStatus.CANCELLED_BY_BARBER


async def test_admin_bookings_list_opens(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    n = len(H.session.calls)
    await H.tap(ADMIN, AdminCb(s="bk", a="list").pack(), message_id=5)
    assert len(H.session.calls) > n


async def test_admin_menu_sections_render(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    for key in (
        "menu.a_barbers",
        "menu.a_services",
        "menu.a_hours",
        "menu.a_timeoff",
        "menu.a_bookings",
        "menu.a_clients",
        "menu.a_stats",
        "menu.a_settings",
        "menu.a_admins",
    ):
        n = len(H.session.calls)
        await H.text(ADMIN, t("uz", key))
        assert len(H.session.calls) > n, key
        await H.text(ADMIN, "/cancel")


async def test_invite_link_approve_flow(world: World) -> None:
    from app.bot.callbacks import LinkCb
    from app.db.uow import write_tx
    from app.services import barbers

    H = Harness(world.sm)  # noqa: N806
    async with write_tx(world.sm) as s:
        b = await barbers.create_barber(s, actor_user_id=world.admin_id, display_name="Sardor")
    tg = 770011
    await H.text(tg, f"/start inv_{b.invite_token}", first_name="Sardor")
    assert any("Sardor" in x for x in H.texts_to(tg))
    async with read_tx(world.sm) as s:
        uid = (await users.load_context(s, tg_id=tg)).user.id
    ok = LinkCb(a="ok", b=b.id, u=uid).pack()
    assert ok in H.last_callbacks(ADMIN)
    # someone else opening the same (forwarded) link is refused
    await H.text(770012, f"/start inv_{b.invite_token}")
    assert t("uz", "err.invalid_invite") in H.texts_to(770012)
    await H.tap(ADMIN, ok, message_id=5)
    async with read_tx(world.sm) as s:
        roles = await users.get_roles(s, uid)
    assert roles.is_barber and roles.barber_id == b.id
    assert H.texts_to(tg)[-1]  # the barber was told


async def test_setup_link_makes_first_admin(sm, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_config
    from app.db.uow import write_tx

    monkeypatch.setattr(type(get_config()), "admin_ids", property(lambda self: frozenset()))
    async with write_tx(sm) as s:
        token = await settings.ensure_setup_token(s)
    H = Harness(sm)  # noqa: N806
    await H.text(3001, f"/start setup_{token}")
    await H.text(3002, f"/start setup_{token}")
    async with read_tx(sm) as s:
        first = await users.load_context(s, tg_id=3001)
        second = await users.load_context(s, tg_id=3002)
    assert first.roles.is_admin and not second.roles.is_admin
    assert t("uz", "err.invalid_invite") in H.texts_to(3002)
