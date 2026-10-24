from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.enums import BookingStatus, CancelledBy
from app.db.uow import read_tx, write_tx
from app.services import barbers, users
from app.services.errors import InputError, InvalidInviteError, PermissionDeniedError
from tests.factories import NOW, D, L, World, book


async def _create(w: World, name: str = "Bekzod"):  # type: ignore[no-untyped-def]
    async with write_tx(w.sm) as s:
        return await barbers.create_barber(s, actor_user_id=w.admin_id, display_name=name, now=NOW)


async def _user(w: World, tg: int) -> int:
    async with write_tx(w.sm) as s:
        return (await users.ensure_user(s, tg_id=tg, tg_username=None, first_name="U")).user.id


async def test_invite_ttl_and_flow(world: World) -> None:
    b = await _create(world)
    assert len(b.invite_token) == 16
    assert b.invite_expires_at == NOW + timedelta(hours=24)
    u = await _user(world, 9001)
    async with write_tx(world.sm) as s:
        req = await barbers.request_link(s, user_id=u, token=b.invite_token, now=NOW)
    assert req.barber.pending_link_user_id == u and req.barber.invite_token is None
    async with read_tx(world.sm) as s:
        assert not (await users.get_roles(s, u)).is_barber
        assert b.id not in [x.id for x in await barbers.list_barbers(s)]
    async with write_tx(world.sm) as s:
        out = await barbers.approve_link(s, actor_user_id=world.admin_id, barber_id=b.id, user_id=u)
    # linked, but hidden from clients until he has working hours [D-1]
    assert out.user_id == u and out.is_active and not out.is_public
    async with read_tx(world.sm) as s:
        roles = await users.get_roles(s, u)
    assert roles.is_barber and roles.barber_id == b.id


async def test_reused_token_and_second_user(world: World) -> None:
    b = await _create(world)
    u1, u2 = await _user(world, 9002), await _user(world, 9003)
    async with write_tx(world.sm) as s:
        await barbers.request_link(s, user_id=u1, token=b.invite_token, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidInviteError):
            await barbers.request_link(s, user_id=u2, token=b.invite_token, now=NOW)
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidInviteError):
            await barbers.approve_link(s, actor_user_id=world.admin_id, barber_id=b.id, user_id=u2)


async def test_expired_token(world: World) -> None:
    b = await _create(world)
    u = await _user(world, 9004)
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidInviteError):
            await barbers.request_link(
                s, user_id=u, token=b.invite_token, now=NOW + timedelta(hours=24)
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidInviteError):
            await barbers.request_link(s, user_id=u, token="nope", now=NOW)


async def test_existing_barber_cannot_claim_second(world: World) -> None:
    b = await _create(world)
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidInviteError):
            await barbers.request_link(s, user_id=world.ali_user_id, token=b.invite_token, now=NOW)


async def test_deny(world: World) -> None:
    b = await _create(world)
    u = await _user(world, 9005)
    async with write_tx(world.sm) as s:
        await barbers.request_link(s, user_id=u, token=b.invite_token, now=NOW)
        out = await barbers.deny_link(s, actor_user_id=world.admin_id, barber_id=b.id, user_id=u)
    assert out.user_id is None and out.pending_link_user_id is None


async def test_non_admin_cannot_manage(world: World) -> None:
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await barbers.create_barber(s, actor_user_id=world.ali_user_id, display_name="X")


async def test_validation(world: World) -> None:
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await barbers.update_barber(
                s, actor_user_id=world.admin_id, barber_id=world.ali_id, buffer_min=10
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await barbers.update_barber(
                s, actor_user_id=world.admin_id, barber_id=world.ali_id, slot_step_min=20
            )


async def test_deactivate_cancels_future_and_hides(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        prev = await barbers.preview_future_bookings(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, now=NOW
        )
    assert [b.id for b in prev] == [r.booking.id]
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await barbers.deactivate_barber(
                s, actor_user_id=world.admin_id, barber_id=world.ali_id, reason=" ", now=NOW
            )
    async with write_tx(world.sm) as s:
        out = await barbers.deactivate_barber(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, reason="ketdi", now=NOW
        )
    assert out[0].status == BookingStatus.CANCELLED_BY_BARBER
    assert out[0].cancelled_by == CancelledBy.ADMIN
    async with read_tx(world.sm) as s:
        assert await barbers.list_barbers(s) == []
        assert not (await users.get_roles(s, world.ali_user_id)).is_barber


async def test_reachability(world: World) -> None:
    async with write_tx(world.sm) as s:
        await barbers.set_reachable(s, barber_id=world.ali_id, reachable=False)
    async with read_tx(world.sm) as s:
        ctx = await users.load_context(s, tg_id=2000)
    assert ctx is not None and ctx.barber_reachable is False
    async with write_tx(world.sm) as s:
        assert await barbers.mark_reachable_by_user(s, user_id=world.ali_user_id)
    async with read_tx(world.sm) as s:
        assert (await barbers.get_barber(s, world.ali_id)).is_reachable
