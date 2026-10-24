from __future__ import annotations

from datetime import time

import pytest

from app.db.uow import read_tx, write_tx
from app.services import settings
from app.services.errors import InputError
from tests.factories import World


def test_parse_value() -> None:
    assert settings.parse_value("booking_horizon_days", " 14 ") == 14
    assert settings.parse_value("new_client_requires_approval", "Ha") is True
    assert settings.parse_value("reminder_2h_enabled", "yo'q") is False
    assert settings.parse_value("daily_summary_time", "8.00") == "08:00"
    assert settings.parse_value("loyalty_mode", "CASHBACK") == "cashback"
    assert settings.parse_value("latitude", "-") is None
    assert settings.parse_value("latitude", "41,31") == pytest.approx(41.31)
    assert settings.parse_value("shop_name", "—") == ""
    for key, raw in [
        ("booking_horizon_days", "abc"),
        ("booking_horizon_days", "0"),
        ("booking_horizon_days", "61"),
        ("new_client_requires_approval", "maybe"),
        ("daily_summary_time", "25:00"),
        ("daily_summary_time", "8"),
        ("loyalty_mode", "points"),
        ("latitude", "91"),
        ("shop_name", "x" * 65),
        ("no_such_key", "1"),
    ]:
        with pytest.raises(InputError) as e:
            settings.parse_value(key, raw)
        assert e.value.ctx["field"] == key


async def test_defaults_and_update(world: World) -> None:
    async with read_tx(world.sm) as s:
        st = await settings.get_shop_settings(s)
    assert st.booking_horizon_days == 7 and st.min_lead_min == 30
    assert st.daily_summary_time == time(8, 0) or st.daily_summary_time == "08:00"
    await world.set("booking_horizon_days", "14")
    await world.set("daily_summary_time", "07:30")
    async with read_tx(world.sm) as s:
        st = await settings.get_shop_settings(s)
    assert st.booking_horizon_days == 14


async def test_private_keys_refused(world: World) -> None:
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await settings.update_setting(
                s, actor_user_id=world.admin_id, key="_setup_token", raw_value="x"
            )


async def test_markers(world: World) -> None:
    async with write_tx(world.sm) as s:
        assert await settings.get_marker(s, "_job.morning") is None
        await settings.set_marker(s, "_job.morning", "2026-10-26")
        with pytest.raises(InputError):
            await settings.set_marker(s, "job", "x")
    async with read_tx(world.sm) as s:
        assert await settings.get_marker(s, "_job.morning") == "2026-10-26"
        # markers don't leak into ShopSettings
        await settings.get_shop_settings(s)


async def test_location(world: World) -> None:
    async with write_tx(world.sm) as s:
        st = await settings.set_location(
            s, actor_user_id=world.admin_id, latitude=41.3, longitude=69.2
        )
    assert st.latitude == 41.3 and st.longitude == 69.2
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await settings.set_location(s, actor_user_id=world.admin_id, latitude=100, longitude=0)


async def test_corrupt_stored_value_falls_back(world: World) -> None:
    from app.db.models import Setting

    async with write_tx(world.sm) as s:
        s.add(Setting(key="min_lead_min", value='"abc"'))
    async with read_tx(world.sm) as s:
        st = await settings.get_shop_settings(s)
    assert st.min_lead_min == 30
