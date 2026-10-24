"""QA regression (security_abuse): bonus / soft-block abuse paths."""

from __future__ import annotations

from datetime import timedelta

from app.config import CONSENT_VERSION
from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, users
from tests.factories import D, L, World, book

PHONE = "+998901234567"


async def _walkin(w: World, day: int, phone: str, *, yes: bool | None, client: int) -> None:
    start = L(D(day), "15:00")
    async with write_tx(w.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=w.ali_user_id,
            barber_id=w.ali_id,
            service_id=w.svc30,
            start_at=start,
            client_name="Walk",
            client_phone=phone,
            now=start - timedelta(minutes=5),
        )
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=w.ali_user_id, now=start + timedelta(minutes=30)
        )
        if yes is not None:
            await bookings.confirm_manual_visit(
                s,
                booking_id=b.id,
                client_user_id=client,
                yes=yes,
                now=start + timedelta(minutes=31),
            )


async def _real_visit(w: World, c: int, day: int):  # type: ignore[no-untyped-def]
    start = L(D(day), "10:00")
    r = await book(w, c, start, now=start - timedelta(hours=2))
    async with write_tx(w.sm) as s:
        return await bookings.complete_booking(
            s,
            booking_id=r.booking.id,
            actor_user_id=w.ali_user_id,
            now=start + timedelta(minutes=30),
        )


async def test_denied_walkin_does_not_cancel_welcome_bonus(world: World) -> None:
    """A barber logs a fake walk-in with a new client's phone; the client says
    'no, I was not there'. Their real first visit must still get the welcome bonus."""
    c = await world.new_client(phone=PHONE)
    await _walkin(world, 0, PHONE, yes=False, client=c)
    res = await _real_visit(world, c, 1)
    assert res.welcome_credited == 1, "denied walk-in counted as the client's first visit"


async def test_unconfirmed_walkin_does_not_cancel_welcome_bonus(world: World) -> None:
    c = await world.new_client(phone=PHONE)
    await _walkin(world, 0, PHONE, yes=None, client=c)  # client never answered
    res = await _real_visit(world, c, 1)
    assert res.welcome_credited == 1


async def test_denied_walkin_does_not_cancel_referrer_bonus(world: World) -> None:
    referrer = await world.new_client()
    async with read_tx(world.sm) as s:
        code = (await users.get_user(s, referrer)).ref_code
    async with write_tx(world.sm) as s:
        ctx = await users.ensure_user(
            s, tg_id=777_001, tg_username=None, first_name="Friend", start_payload=f"ref_{code}"
        )
        friend = ctx.user.id
        await users.accept_consent(s, user_id=friend, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=friend, raw_phone=PHONE)
    await _walkin(world, 0, PHONE, yes=False, client=friend)
    res = await _real_visit(world, friend, 1)
    assert res.referral_credited_to == referrer


async def test_delete_me_and_rejoin_does_not_reset_soft_block(world: World) -> None:
    """A client with 3 no-shows (soft-blocked: every booking needs approval) runs
    /delete_me, starts the bot again, shares the same phone, and books with
    instant confirmation again."""
    async with write_tx(world.sm) as s:
        ctx = await users.ensure_user(s, tg_id=777_002, tg_username=None, first_name="Nos")
        c = ctx.user.id
        await users.accept_consent(s, user_id=c, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=c, raw_phone=PHONE)
    for d in (0, 1, 2):
        start = L(D(d), "10:00")
        r = await book(world, c, start, now=start - timedelta(hours=1))
        async with write_tx(world.sm) as s:
            await bookings.mark_no_show(
                s,
                booking_id=r.booking.id,
                actor_user_id=world.ali_user_id,
                now=start + timedelta(minutes=20),
            )
    async with read_tx(world.sm) as s:
        assert await users.is_soft_blocked(s, c, now=L(D(3), "08:00"))
    async with write_tx(world.sm) as s:
        await users.delete_me(s, user_id=c, now=L(D(3), "08:00"))
    async with write_tx(world.sm) as s:
        ctx = await users.ensure_user(s, tg_id=777_002, tg_username=None, first_name="Nos")
        c2 = ctx.user.id
        await users.accept_consent(s, user_id=c2, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=c2, raw_phone=PHONE)
    start = L(D(4), "11:00")
    r = await book(world, c2, start, now=L(D(3), "09:00"))
    assert r.client_soft_blocked and r.booking.status == BookingStatus.PENDING, (
        "no-show history wiped by /delete_me + rejoin"
    )
