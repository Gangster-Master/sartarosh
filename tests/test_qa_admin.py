"""QA regression tests: ADMIN journeys (each was a confirmed bug, now fixed)."""

from __future__ import annotations

import math
from datetime import time

import pytest

from app.bot.callbacks import AdminCb
from app.bot.i18n import t
from app.bot.states import AdminHoursFlow
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, catalog, schedule, settings, users
from app.services.errors import InputError
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World, book

ADMIN = 1000


def wh(a: str, bid: int, v: str = "") -> str:
    return AdminCb(s="wh", a=a, id=bid, v=v).pack()


async def _week(w: World, bid: int) -> dict[int, tuple]:  # type: ignore[type-arg]
    async with read_tx(w.sm) as s:
        return {
            x.weekday: (x.start_time, x.end_time, x.break_start, x.break_end)
            for x in await schedule.get_week_hours(s, bid)
        }


# ---------------------------------------------------------------- hours


async def test_admin_stale_break_button_of_other_barber_must_not_save(world: World) -> None:
    """D-4: break buttons ignore the barber id they carry."""
    other, _ = await world.new_barber("Bekhzod")
    before_ali = await _week(world, world.ali_id)
    before_other = await _week(world, other)
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, wh("wd", world.ali_id, "2"), message_id=5)
    await H.tap(ADMIN, wh("rng", world.ali_id, "2|0900-1800"), message_id=5)
    # a "no break" button left on an older screen for the OTHER barber
    await H.tap(ADMIN, wh("nobr", other), message_id=4)
    assert await _week(world, world.ali_id) == before_ali
    assert await _week(world, other) == before_other


async def test_admin_copy_scope_after_source_day_turned_off_must_not_wipe_week(
    world: World,
) -> None:
    """The scope screen promised 'Wed 10:00–20:00'; if Wed is now off the copy wipes Mon–Sat."""
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, wh("copy", world.ali_id), message_id=5)
    await H.tap(ADMIN, wh("copyfrom", world.ali_id, "2"), message_id=5)
    assert wh("cp", world.ali_id, "2-0") in H.last_callbacks(ADMIN)
    # meanwhile (another admin / another screen) Wednesday becomes a day off
    async with write_tx(world.sm) as s:
        await schedule.set_working_hours(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            weekday=2,
            start_time=None,
            end_time=None,
            now=NOW,
        )
    await H.tap(ADMIN, wh("cp", world.ali_id, "2-0"), message_id=5)
    week = await _week(world, world.ali_id)
    assert sorted(week) == [0, 1, 3, 4, 5], "copying an empty source day wiped the barber's week"


async def test_admin_copy_from_out_of_range_weekday_must_not_wipe_week(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, wh("cp", world.ali_id, "9-1"), message_id=5)
    assert sorted(await _week(world, world.ali_id)) == [0, 1, 2, 3, 4, 5]


async def test_admin_typed_off_grid_range_is_rejected_at_range_step(world: World) -> None:
    """'09:10-18:00' is accepted at the range step, then every break choice fails on the
    start time and the admin is stuck in the break step."""
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, wh("wd", world.ali_id, "2"), message_id=5)
    await H.text(ADMIN, "09:10-18:00")
    assert await H.fsm_state(ADMIN) == AdminHoursFlow.start.state, (
        "off-grid range moved on to the break step"
    )
    # and the admin can recover by typing a valid range
    await H.text(ADMIN, "09:00-18:00")
    await H.tap(ADMIN, wh("nobr", world.ali_id), message_id=5)
    assert (await _week(world, world.ali_id))[2] == (time(9), time(18), None, None)


# ---------------------------------------------------------------- bookings search


async def test_admin_bookings_search_finds_cyrillic_client_name(world: World) -> None:
    """SQLite lower() is ASCII-only: a Russian/Uzbek-Cyrillic name typed as stored is not found."""
    cid = await world.new_client(name="Алишер")
    await book(world, cid, L(D(1), "11:00"))
    async with read_tx(world.sm) as s:
        exact = await bookings.list_bookings(
            s, actor_user_id=world.admin_id, phone_or_name="Алишер"
        )
        lower = await bookings.list_bookings(
            s, actor_user_id=world.admin_id, phone_or_name="алишер"
        )
    assert exact.total == 1
    assert lower.total == 1


# ---------------------------------------------------------------- settings


async def test_admin_loyalty_mode_switch_not_blocked_by_deleted_users_balance(
    world: World,
) -> None:
    """A deleted account keeps bonus_balance; nobody can find it to zero it, so the
    loyalty mode can never be switched again."""
    cid = await world.new_client(name="Gone")
    async with write_tx(world.sm) as s:
        await bonus.adjust_bonus(
            s, actor_user_id=world.admin_id, user_id=cid, delta=2, comment="gift"
        )
        await users.delete_me(s, user_id=cid, now=NOW)
    async with write_tx(world.sm) as s:
        # no live client has a balance; the switch must be allowed
        await settings.update_setting(
            s, actor_user_id=world.admin_id, key="loyalty_mode", raw_value="cashback"
        )
        assert (await settings.get_shop_settings(s)).loyalty_mode == "cashback"


async def test_admin_latitude_nan_rejected(world: World) -> None:
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await settings.update_setting(
                s, actor_user_id=world.admin_id, key="latitude", raw_value="nan"
            )
    async with read_tx(world.sm) as s:
        lat = (await settings.get_shop_settings(s)).latitude
    assert lat is None or not math.isnan(lat)


# ---------------------------------------------------------------- barber services


async def test_admin_saving_barber_services_keeps_temporarily_inactive_service(
    world: World,
) -> None:
    async with write_tx(world.sm) as s:
        await catalog.set_barber_services(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            service_ids=[world.svc30, world.svc60],
        )
        await catalog.set_barber_service_override(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            service_id=world.svc60,
            price_uzs=90_000,
            duration_min=None,
        )
        await catalog.update_service(
            s, actor_user_id=world.admin_id, service_id=world.svc60, is_active=False
        )
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, AdminCb(s="br", a="svc", id=world.ali_id).pack(), message_id=5)
    await H.tap(ADMIN, AdminCb(s="br", a="svcs", id=world.ali_id).pack(), message_id=5)
    async with write_tx(world.sm) as s:
        await catalog.update_service(
            s, actor_user_id=world.admin_id, service_id=world.svc60, is_active=True
        )
    async with read_tx(world.sm) as s:
        offered = {x.service_id: x for x in await catalog.list_barber_services(s, world.ali_id)}
    assert world.svc60 in offered, "saving the menu dropped the inactive service from the barber"
    assert offered[world.svc60].price_uzs == 90_000


# ---------------------------------------------------------------- admins


async def test_admin_stale_demote_does_not_message_non_admin(world: World) -> None:
    cid = await world.new_client(name="Plain")
    async with read_tx(world.sm) as s:
        tg = (await users.get_user(s, cid)).tg_id
    H = Harness(world.sm)  # noqa: N806
    await H.tap(ADMIN, AdminCb(s="adm", a="del", id=cid, v="ok").pack(), message_id=5)
    assert t("uz", "admin.adm.you_demoted") not in H.texts_to(tg), (
        "a user who never was an admin was told he was demoted"
    )


# ---------------------------------------------------------------- blocked users as staff


async def test_admin_reactivating_barber_of_blocked_user_does_not_publish_him(
    world: World,
) -> None:
    """set_blocked refuses staff, but a deactivated barber's user is not 'staff', can be
    blocked, and reactivation then puts a blocked (bot-locked) user back in front of clients."""
    from app.services import barbers

    bid, uid = await world.new_barber("Bekhzod")
    async with write_tx(world.sm) as s:
        await barbers.deactivate_barber(
            s, actor_user_id=world.admin_id, barber_id=bid, reason="left", now=NOW
        )
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=uid, blocked=True, now=NOW)
    async with write_tx(world.sm) as s:
        try:
            await barbers.activate_barber(s, actor_user_id=world.admin_id, barber_id=bid)
        except InputError:
            pass  # refusing is fine too
    async with read_tx(world.sm) as s:
        public = {b.id for b in await barbers.list_barbers(s, public_only=True)}
    assert bid not in public, "a blocked user (who cannot use the bot) is a public, bookable barber"


async def test_admin_cannot_promote_blocked_user(world: World) -> None:
    cid = await world.new_client(name="Bad")
    async with write_tx(world.sm) as s:
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=cid, blocked=True, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await users.set_admin(s, actor_user_id=world.admin_id, user_id=cid, is_admin=True)
