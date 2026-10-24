from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.enums import BookingStatus
from app.db.models import AuditLog
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, settings, users
from app.services.errors import (
    InputError,
    InvalidInviteError,
    InvalidPhoneError,
    PermissionDeniedError,
)
from tests.factories import NOW, D, L, World, book


async def test_env_vs_promoted_admin(world: World) -> None:
    c = await world.new_client()
    async with write_tx(world.sm) as s:
        await users.set_admin(s, actor_user_id=world.admin_id, user_id=c, is_admin=True)
    async with read_tx(world.sm) as s:
        r = await users.get_roles(s, c)
        env = await users.get_roles(s, world.admin_id)
        admins = await users.list_admins(s, actor_user_id=world.admin_id)
    assert r.is_admin and not r.is_env_admin
    assert env.is_admin and env.is_env_admin
    assert {a.id for a in admins} == {world.admin_id, c}
    # demote works
    async with write_tx(world.sm) as s:
        await users.set_admin(s, actor_user_id=world.admin_id, user_id=c, is_admin=False)
    async with read_tx(world.sm) as s:
        assert not (await users.get_roles(s, c)).is_admin
    # env admin can't be demoted [G2]
    async with write_tx(world.sm) as s:
        await users.set_admin(s, actor_user_id=world.admin_id, user_id=c, is_admin=True)
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError) as e:
            await users.set_admin(s, actor_user_id=c, user_id=world.admin_id, is_admin=False)
    assert e.value.ctx["field"] == "env_admin"
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await users.set_admin(s, actor_user_id=c, user_id=c, is_admin=False)


async def test_setup_token_flow(sm, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_config

    monkeypatch.setattr(type(get_config()), "admin_ids", property(lambda self: frozenset()))
    async with write_tx(sm) as s:
        token = await settings.ensure_setup_token(s)
        u1 = (await users.ensure_user(s, tg_id=1, tg_username=None, first_name="A")).user.id
        u2 = (await users.ensure_user(s, tg_id=2, tg_username=None, first_name="B")).user.id
    assert token
    async with write_tx(sm) as s:
        assert await settings.ensure_setup_token(s) == token  # stable while no admin
    async with write_tx(sm) as s:
        with pytest.raises(InvalidInviteError):
            await users.claim_setup_token(s, user_id=u1, token="wrong")
    async with write_tx(sm) as s:
        u = await users.claim_setup_token(s, user_id=u1, token=token)
    assert u.is_admin
    async with write_tx(sm) as s:
        with pytest.raises(InvalidInviteError):
            await users.claim_setup_token(s, user_id=u2, token=token)
    async with write_tx(sm) as s:
        assert await settings.ensure_setup_token(s) is None


async def test_block_cancels_and_guard_rails(world: World) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(1), "10:00"))
    async with write_tx(world.sm) as s:
        out = await users.set_blocked(
            s, actor_user_id=world.admin_id, user_id=c, blocked=True, now=NOW
        )
    assert [b.id for b in out] == [r.booking.id]
    assert out[0].status == BookingStatus.CANCELLED_BY_BARBER and out[0].cancel_reason == "blocked"
    for target in (world.admin_id, world.ali_user_id):
        async with write_tx(world.sm) as s:
            with pytest.raises(PermissionDeniedError):
                await users.set_blocked(
                    s, actor_user_id=world.admin_id, user_id=target, blocked=True, now=NOW
                )
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await users.set_blocked(s, actor_user_id=c, user_id=world.ali_user_id, blocked=True)
    async with write_tx(world.sm) as s:
        assert (
            await users.set_blocked(
                s, actor_user_id=world.admin_id, user_id=c, blocked=False, now=NOW
            )
            == []
        )


async def test_delete_me_anonymises_and_keeps_counts(world: World) -> None:
    c = await world.new_client(name="Secret")
    start = L(D(0), "10:00")
    done = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=done.booking.id, actor_user_id=world.ali_user_id, now=start
        )
    future = await book(world, c, L(D(2), "10:00"), now=start)
    async with write_tx(world.sm) as s:
        out = await users.delete_me(s, user_id=c, now=L(D(0), "11:00"))
    assert [b.id for b in out] == [future.booking.id]
    async with read_tx(world.sm) as s:
        u = await users.get_user(s, c)
        b = await bookings.get_booking(s, done.booking.id)
        st = await bonus.get_bonus_status(s, c)
        ctx = await users.load_context(s, tg_id=u.tg_id)
    assert u.tg_id == -c and u.phone is None and u.first_name == "" and u.full_name is None
    assert b.client_name == "—" and b.client_phone is None and b.client_tg_id is None
    assert b.status == BookingStatus.COMPLETED
    assert st.balance == 2  # ledger kept
    assert ctx is not None and ctx.roles.is_admin is False


async def test_phone_search_audited(world: World) -> None:
    await world.new_client(phone="+998901234567")
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await users.find_users_by_phone(s, actor_user_id=world.admin_id, phone_query="45")
    async with write_tx(world.sm) as s:
        found = await users.find_users_by_phone(s, actor_user_id=world.admin_id, phone_query="4567")
    assert len(found) == 1
    async with read_tx(world.sm) as s:
        acts = (await s.execute(select(AuditLog.action))).scalars().all()
    assert "client.search" in acts
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await users.find_users_by_phone(s, actor_user_id=world.ali_user_id, phone_query="4567")


async def test_phone_search_limit_10(world: World) -> None:
    for i in range(12):
        await world.new_client(phone=f"+99890{i:03d}4321")
    async with write_tx(world.sm) as s:
        found = await users.find_users_by_phone(s, actor_user_id=world.admin_id, phone_query="4321")
    assert len(found) == 10


async def test_client_profile(world: World) -> None:
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    async with read_tx(world.sm) as s:
        p = await users.get_client_profile(s, actor_user_id=world.admin_id, user_id=c)
    assert p.stats.is_new and len(p.recent_bookings) == 1 and not p.soft_blocked


async def test_contact_foreign(world: World) -> None:
    c = await world.new_client()
    await world.set("allow_foreign_phone", "false")
    async with write_tx(world.sm) as s:
        with pytest.raises(InvalidPhoneError):
            await users.set_verified_contact(s, user_id=c, raw_phone="+79161234567")
    await world.set("allow_foreign_phone", "true")
    async with write_tx(world.sm) as s:
        r = await users.set_verified_contact(s, user_id=c, raw_phone="+79161234567")
    assert r.is_foreign and r.user.phone_verified


async def test_duplicates_reported(world: World) -> None:
    a = await world.new_client(phone="+998901112233")
    b = await world.new_client(phone="+998909998877")
    async with write_tx(world.sm) as s:
        r = await users.set_verified_contact(s, user_id=b, raw_phone="+998901112233")
    assert r.duplicate_user_ids == [a]


async def test_ensure_user_lang_and_src(sm) -> None:  # type: ignore[no-untyped-def]
    async with write_tx(sm) as s:
        ru = await users.ensure_user(
            s, tg_id=11, tg_username=None, first_name="R", language_code="ru"
        )
        en = await users.ensure_user(
            s,
            tg_id=12,
            tg_username=None,
            first_name="E",
            language_code="en",
            start_payload="src_insta",
        )
        bad = await users.ensure_user(
            s, tg_id=13, tg_username=None, first_name="E", start_payload="src_BAD TAG!"
        )
    assert ru.user.lang == "ru" and en.user.lang == "uz"
    assert not ru.user.lang_chosen
    from app.db.models import User

    async with read_tx(sm) as s:
        assert (await s.get(User, en.user.id)).acq_source == "insta"
        assert (await s.get(User, bad.user.id)).acq_source is None


async def test_needs_sync(sm) -> None:  # type: ignore[no-untyped-def]
    async with write_tx(sm) as s:
        ctx = await users.ensure_user(s, tg_id=21, tg_username="x", first_name="X")
    assert not users.needs_sync(ctx.user, tg_username="x", first_name="X")
    assert users.needs_sync(ctx.user, tg_username="y", first_name="X")
    assert users.needs_sync(ctx.user, tg_username="x", first_name="Z")
