from __future__ import annotations

import pytest

from app.db.uow import read_tx, write_tx
from app.services import catalog
from app.services.errors import InputError
from tests.factories import World


async def test_service_validation(world: World) -> None:
    for dur, price in ((20, 1000), (0, 1000), (255, 1000), (30, -1)):
        async with write_tx(world.sm) as s:
            with pytest.raises(InputError):
                await catalog.create_service(
                    s, actor_user_id=world.admin_id, name="X", duration_min=dur, price_uzs=price
                )


async def test_deactivated_service_hidden(world: World) -> None:
    async with write_tx(world.sm) as s:
        await catalog.update_service(
            s, actor_user_id=world.admin_id, service_id=world.svc60, is_active=False
        )
    async with read_tx(world.sm) as s:
        bookable = [x.id for x in await catalog.list_bookable_services(s)]
        offered = [x.service_id for x in await catalog.list_barber_services(s, world.ali_id)]
        all_ = [x.id for x in await catalog.list_services(s, active_only=False)]
    assert bookable == [world.svc30] and offered == [world.svc30]
    assert world.svc60 in all_
    async with read_tx(world.sm) as s:
        with pytest.raises(InputError):
            await catalog.resolve_service(s, barber_id=world.ali_id, service_id=world.svc60)


async def test_barber_specific_services_and_override(world: World) -> None:
    bek, _ = await world.new_barber("Bekzod")
    async with write_tx(world.sm) as s:
        await catalog.set_barber_services(
            s, actor_user_id=world.admin_id, barber_id=bek, service_ids=[world.svc30]
        )
    async with read_tx(world.sm) as s:
        for_60 = [
            b.display_name
            for b in await catalog.list_barbers_for_service(s, service_id=world.svc60)
        ]
    assert for_60 == ["Ali"]
    async with write_tx(world.sm) as s:
        rs = await catalog.set_barber_service_override(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            price_uzs=70_000,
            duration_min=45,
        )
    assert rs.price_uzs == 70_000 and rs.duration_min == 45
    async with read_tx(world.sm) as s:
        offered = {x.service_id for x in await catalog.list_barber_services(s, world.ali_id)}
    assert offered == {world.svc30, world.svc60}  # override did not restrict the offer
