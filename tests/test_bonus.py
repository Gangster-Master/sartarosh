from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.enums import BonusReason
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, users
from app.services.errors import InputError, InsufficientBonusError, PermissionDeniedError
from tests.factories import NOW, D, L, World, book, new_client


async def _visit(w: World, c: int, day: int, hhmm: str = "10:00", *, use_bonus: bool = False):  # type: ignore[no-untyped-def]
    start = L(D(day), hhmm)
    r = await book(w, c, start, now=start - timedelta(hours=2), use_bonus=use_bonus)
    async with write_tx(w.sm) as s:
        return await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=w.ali_user_id,
            now=start + timedelta(minutes=30),
        )


async def _status(w: World, c: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        return await bonus.get_bonus_status(s, c)


async def test_five_visits_then_free(world: World) -> None:
    await world.set("welcome_bonus", "0")
    c = await world.new_client()
    for d in range(5):
        r = await _visit(world, c, d)
        assert r.earned == 1
    st = await _status(world, c)
    assert st.balance == 5 and st.available == 5 and st.can_redeem and st.progress == 5
    r = await _visit(world, c, 5, use_bonus=True)
    assert r.redeemed and not r.bonus_failed and r.earned == 0
    assert r.booking.final_price_uzs == 0 and r.booking.bonus_redeemed
    st = await _status(world, c)
    assert st.balance == 0


async def test_welcome_on_first_visit_only(world: World) -> None:
    c = await world.new_client()
    r1 = await _visit(world, c, 0)
    assert r1.earned == 1 and r1.welcome_credited == 1 and r1.new_balance == 2
    st = await _status(world, c)
    assert (st.progress, st.required) == (2, 5)
    r2 = await _visit(world, c, 1)
    assert r2.welcome_credited == 0 and r2.new_balance == 3


async def test_same_day_earns_once(world: World) -> None:
    c = await world.new_client()
    r1 = await _visit(world, c, 0, "10:00")
    r2 = await _visit(world, c, 0, "15:00")
    assert r1.earned == 1 and r2.earned == 0


async def test_no_show_earns_nothing(world: World) -> None:
    c = await world.new_client()
    start = L(D(0), "10:00")
    r = await book(world, c, start)
    async with write_tx(world.sm) as s:
        await bookings.mark_no_show(
            s,
            booking_id=r.booking.id,
            actor_user_id=world.ali_user_id,
            now=start + timedelta(minutes=20),
        )
    assert (await _status(world, c)).balance == 0


async def test_reserved_and_insufficient(world: World) -> None:
    await world.set("welcome_bonus", "0")
    await world.set("max_active_bookings", "5")
    c = await world.new_client()
    for d in range(5):
        await _visit(world, c, d)
    # one active bonus booking reserves 5 -> a second bonus booking is impossible
    r = await book(world, c, L(D(5), "15:00"), now=L(D(5), "08:00"), use_bonus=True)
    assert r.booking.bonus_cost == 5 and r.booking.final_price_uzs == 0
    st = await _status(world, c)
    assert st.reserved == 5 and st.available == 0 and not st.can_redeem
    with pytest.raises(InsufficientBonusError):
        await book(world, c, L(D(5), "17:00"), now=L(D(5), "08:00"), use_bonus=True)
    # cancelling the bonus booking leaves the balance unchanged
    async with write_tx(world.sm) as s:
        await bookings.cancel_by_client(
            s, booking_id=r.booking.id, client_user_id=c, now=L(D(5), "08:00")
        )
    st = await _status(world, c)
    assert st.balance == 5 and st.reserved == 0


async def test_bonus_failed_when_required_raised(world: World) -> None:
    await world.set("welcome_bonus", "0")
    c = await world.new_client()
    for d in range(5):
        await _visit(world, c, d)
    start = L(D(5), "10:00")
    r = await book(world, c, start, now=start - timedelta(hours=2), use_bonus=True)
    assert r.booking.bonus_cost == 5
    await world.set("bonus_required_stamps", "10")  # does not affect the snapshot
    # balance taken away meanwhile (admin adjust) -> completion still works
    async with write_tx(world.sm) as s:
        await bonus.adjust_bonus(
            s, actor_user_id=world.admin_id, user_id=c, delta=-1, comment="fix"
        )
    async with write_tx(world.sm) as s:
        res = await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=start
        )
    assert res.bonus_failed and not res.redeemed
    assert res.booking.final_price_uzs == 60_000 and not res.booking.bonus_redeemed
    assert res.earned == 1  # a full-price visit earns its stamp
    assert (await _status(world, c)).balance == 5


async def test_snapshot_cost_survives_required_change(world: World) -> None:
    await world.set("welcome_bonus", "0")
    c = await world.new_client()
    for d in range(5):
        await _visit(world, c, d)
    start = L(D(5), "10:00")
    r = await book(world, c, start, now=start - timedelta(hours=2), use_bonus=True)
    await world.set("bonus_required_stamps", "10")
    async with write_tx(world.sm) as s:
        res = await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=start
        )
    assert res.redeemed and (await _status(world, c)).balance == 0


async def test_percent_reward(world: World) -> None:
    await world.set("bonus_reward_type", "percent")
    await world.set("bonus_reward_value", "50")
    await world.set("welcome_bonus", "5")
    c = await world.new_client()
    await _visit(world, c, 0)
    async with read_tx(world.sm) as s:
        q = await bonus.quote_redeem(s, user_id=c, price_uzs=60_000)
    assert q.possible and q.cost == 5 and q.final_price_uzs == 30_000 and q.discount_uzs == 30_000


async def test_adjust_requires_comment_and_non_negative(world: World) -> None:
    c = await world.new_client()
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await bonus.adjust_bonus(
                s, actor_user_id=world.admin_id, user_id=c, delta=3, comment=" "
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(InputError):
            await bonus.adjust_bonus(
                s, actor_user_id=world.admin_id, user_id=c, delta=-1, comment="x"
            )
    async with write_tx(world.sm) as s:
        with pytest.raises(PermissionDeniedError):
            await bonus.adjust_bonus(s, actor_user_id=c, user_id=c, delta=3, comment="me")
    async with write_tx(world.sm) as s:
        st = await bonus.adjust_bonus(
            s, actor_user_id=world.admin_id, user_id=c, delta=3, comment="gift"
        )
    assert st.balance == 3 and st.recent[0].reason == BonusReason.MANUAL
    from sqlalchemy import select

    from app.db.models import AuditLog

    async with read_tx(world.sm) as s:
        acts = (await s.execute(select(AuditLog.action))).scalars().all()
    assert "bonus.adjust" in acts


async def test_mode_switch_refused_with_balances(world: World) -> None:
    c = await world.new_client()
    await _visit(world, c, 0)
    with pytest.raises(InputError) as e:
        await world.set("loyalty_mode", "cashback")
    assert e.value.ctx["reason"] == "balances_exist"


async def test_cashback_quote_and_earn(world: World) -> None:
    await world.set("loyalty_mode", "cashback")
    await world.set("welcome_bonus", "0")
    c = await world.new_client()
    r = await _visit(world, c, 0)
    assert r.earned == 3_000  # 5% of 60 000
    async with read_tx(world.sm) as s:
        q = await bonus.quote_redeem(s, user_id=c, price_uzs=60_000)
    assert q.possible and q.cost == 3_000 and q.final_price_uzs == 57_000


async def test_referral_credit_rules(world: World) -> None:
    ref = await world.new_client(name="Referrer")
    async with read_tx(world.sm) as s:
        code = (await users.get_user(s, ref)).ref_code
    # friend joins through the link
    from app.config import CONSENT_VERSION
    from app.db.uow import write_tx as wtx

    async with wtx(world.sm) as s:
        ctx = await users.ensure_user(
            s, tg_id=777001, tg_username=None, first_name="Friend", start_payload=f"ref_{code}"
        )
        fid = ctx.user.id
        assert ctx.user.referrer_id == ref
        await users.accept_consent(s, user_id=fid, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=fid, raw_phone="+998907770001")
    r = await _visit(world, fid, 0)
    assert r.referral_credited_to == ref
    r2 = await _visit(world, fid, 1)
    assert r2.referral_credited_to is None
    assert (await _status(world, ref)).balance == 1
    # own code ignored
    async with wtx(world.sm) as s:
        me = await users.ensure_user(s, tg_id=777002, tg_username=None, first_name="Me")
        code2 = me.user.ref_code
    async with wtx(world.sm) as s:
        again = await users.ensure_user(
            s, tg_id=777002, tg_username=None, first_name="Me", start_payload=f"ref_{code2}"
        )
    assert again.user.referrer_id is None


async def test_referral_not_on_auto_completed(world: World) -> None:
    ref = await world.new_client(name="Referrer")
    async with read_tx(world.sm) as s:
        code = (await users.get_user(s, ref)).ref_code
    from app.config import CONSENT_VERSION

    async with write_tx(world.sm) as s:
        ctx = await users.ensure_user(
            s, tg_id=777003, tg_username=None, first_name="Friend", start_payload=f"ref_{code}"
        )
        fid = ctx.user.id
        await users.accept_consent(s, user_id=fid, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=fid, raw_phone="+998907770003")
    start = L(D(0), "10:00")
    await book(world, fid, start)
    async with write_tx(world.sm) as s:
        done = await bookings.auto_complete_due(s, now=start + timedelta(hours=14))
    assert done[0].referral_credited_to is None
    assert (await _status(world, ref)).balance == 0


async def test_recompute_matches_cached(world: World) -> None:
    c = await world.new_client()
    for d in range(3):
        await _visit(world, c, d)
    cached = (await _status(world, c)).balance
    async with write_tx(world.sm) as s:
        assert await bonus.recompute_balance(s, c) == cached == 4


async def test_unused_import_guard() -> None:
    assert NOW and new_client
