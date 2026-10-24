"""A freshly approved barber has no hours: the admin must be able to turn days into
working days with taps, and clients must not hit a dead end meanwhile."""

from __future__ import annotations

from datetime import time

from app.bot.callbacks import AdminCb, LinkCb
from app.bot.i18n import t
from app.db.uow import read_tx, write_tx
from app.services import barbers, catalog, schedule, users
from tests.bot_harness import Harness
from tests.factories import NOW, World

ADMIN = 1000


def cb(a: str, bid: int, v: str = "") -> str:
    return AdminCb(s="wh", a=a, id=bid, v=v).pack()


async def _week(w: World, bid: int) -> dict[int, tuple]:  # type: ignore[type-arg]
    async with read_tx(w.sm) as s:
        return {
            x.weekday: (x.start_time, x.end_time, x.break_start, x.break_end)
            for x in await schedule.get_week_hours(s, bid)
        }


async def _client_visible(w: World, bid: int) -> bool:
    async with read_tx(w.sm) as s:
        a = {b.id for b in await catalog.list_barbers_for_service(s, service_id=None)}
        b = {b.id for b in await barbers.list_barbers(s, public_only=True)}
    assert (bid in a) == (bid in b)
    return bid in a


async def test_hourless_barber_hidden_from_clients_until_hours_set(world: World) -> None:
    bid, _ = await world.new_barber("Bekhzod", hours=False)
    assert not await _client_visible(world, bid)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, cb("pick", bid), message_id=5)
    assert t("uz", "admin.wh.no_hours") in H.texts_to(ADMIN)[-1]
    assert cb("std", bid) in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, cb("std", bid), message_id=5)
    week = await _week(world, bid)
    assert sorted(week) == [0, 1, 2, 3, 4, 5]
    assert week[0] == (time(10), time(20), time(13), time(14))
    assert await _client_visible(world, bid)
    # a stale second tap must not overwrite a schedule edited meanwhile
    async with write_tx(world.sm) as s:
        await schedule.set_working_hours(
            s,
            actor_user_id=world.admin_id,
            barber_id=bid,
            weekday=0,
            start_time=time(9),
            end_time=time(18),
            now=NOW,
        )
    await H.tap(ADMIN, cb("std", bid), message_id=5)
    assert (await _week(world, bid))[0][:2] == (time(9), time(18))


async def test_day_off_back_to_working_day_with_preset_buttons(world: World) -> None:
    bid, _ = await world.new_barber("Bekhzod", hours=False)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, cb("pick", bid), message_id=5)
    await H.tap(ADMIN, cb("wd", bid, "2"), message_id=5)
    preset = cb("rng", bid, "2|1000-2000")
    assert preset in H.last_callbacks(ADMIN)
    # a preset for another weekday (old message) is stale
    await H.tap(ADMIN, cb("rng", bid, "3|0900-1800"), message_id=5)
    assert await _week(world, bid) == {}
    await H.tap(ADMIN, preset, message_id=5)
    assert cb("brk", bid, "1300-1400") in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, cb("brk", bid, "1300-1400"), message_id=5)
    assert (await _week(world, bid))[2] == (time(10), time(20), time(13), time(14))
    # off again, then back to a working day by typing, without break
    await H.tap(ADMIN, cb("off", bid, "2"), message_id=5)
    assert 2 not in await _week(world, bid)
    await H.tap(ADMIN, cb("wd", bid, "2"), message_id=5)
    await H.text(ADMIN, "9-18")
    await H.tap(ADMIN, cb("nobr", bid), message_id=5)
    assert (await _week(world, bid))[2] == (time(9), time(18), None, None)


async def test_copy_schedule_from_another_barber(world: World) -> None:
    bid, _ = await world.new_barber("Bekhzod", hours=False)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, cb("cpb", bid), message_id=5)
    pick = cb("cpbg", bid, str(world.ali_id))
    assert pick in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, pick, message_id=5)
    assert await _week(world, bid) == await _week(world, world.ali_id)


async def test_approval_and_card_point_to_hours(world: World) -> None:
    async with write_tx(world.sm) as s:
        ctx = await users.ensure_user(s, tg_id=7777, tg_username=None, first_name="Bekhzod")
        b = await barbers.create_barber(
            s, actor_user_id=world.admin_id, display_name="Bekhzod", now=NOW
        )
        await barbers.request_link(s, user_id=ctx.user.id, token=b.invite_token, now=NOW)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, LinkCb(a="ok", b=b.id, u=ctx.user.id).pack(), message_id=5)
    assert t("uz", "admin.br.no_hours") in H.texts_to(ADMIN)[-1]
    assert cb("pick", b.id) in H.last_callbacks(ADMIN)
    await H.tap(ADMIN, AdminCb(s="br", a="pick", id=b.id).pack(), message_id=5)
    assert t("uz", "admin.br.no_hours") in H.texts_to(ADMIN)[-1]
