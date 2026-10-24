"""Regression tests for findings fixed in the final review (docs/STATUS.md)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import CONSENT_VERSION
from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, users
from app.services.errors import TooLateError
from tests.factories import D, L, World, book


async def test_late_accept_refused_when_client_rebooked_same_time(world: World) -> None:
    w = world
    async with write_tx(w.sm) as s:
        await barbers.update_barber(
            s, actor_user_id=w.admin_id, barber_id=w.ali_id, auto_accept=False
        )
    bek, _ = await w.new_barber("Bek", auto_accept=True)
    c = await w.new_client()
    start = L(D(1), "11:00")
    r = await book(w, c, start)
    assert r.booking.status == BookingStatus.PENDING
    exp = r.booking.expire_at
    async with write_tx(w.sm) as s:
        out = await bookings.expire_due_pending(s, now=exp + timedelta(seconds=1))
    assert out[0].status == BookingStatus.EXPIRED
    r2 = await book(w, c, start, barber_id=bek, now=exp + timedelta(minutes=1))
    assert r2.booking.status == BookingStatus.CONFIRMED
    with pytest.raises(TooLateError):
        async with write_tx(w.sm) as s:
            await bookings.confirm_booking(
                s,
                booking_id=r.booking.id,
                actor_user_id=w.ali_user_id,
                now=exp + timedelta(minutes=2),
            )


async def _register(w: World, tg_id: int, phone: str) -> int:
    async with write_tx(w.sm) as s:
        ctx = await users.ensure_user(s, tg_id=tg_id, tg_username=None, first_name="Xasan")
        await users.accept_consent(s, user_id=ctx.user.id, version=CONSENT_VERSION)
        await users.set_verified_contact(s, user_id=ctx.user.id, raw_phone=phone)
    return ctx.user.id


async def test_delete_me_does_not_repay_welcome(world: World) -> None:
    w = world
    await w.set("welcome_bonus", "3")
    u1 = await _register(w, 777, "+998901112233")
    r = await book(w, u1, L(D(0), "11:00"))
    async with write_tx(w.sm) as s:
        cr = await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=w.ali_user_id, now=L(D(0), "11:10")
        )
    assert cr.welcome_credited == 3
    async with write_tx(w.sm) as s:
        await users.delete_me(s, user_id=u1)
    u2 = await _register(w, 777, "+998901112233")
    assert u2 != u1
    r = await book(w, u2, L(D(1), "11:00"))
    async with write_tx(w.sm) as s:
        cr = await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=w.ali_user_id, now=L(D(1), "11:10")
        )
    assert cr.welcome_credited == 0


async def test_proposal_expiry_tells_client_they_did_not_answer(world: World) -> None:
    from tests.bot_harness import Harness

    w = world
    H = Harness(w.sm)  # noqa: N806
    await w.set("new_client_requires_approval", "true")
    c = await w.new_client()
    r = await book(w, c, L(D(1), "10:30"))
    async with write_tx(w.sm) as s:
        p = await bookings.propose_time(
            s, booking_id=r.booking.id, actor_user_id=w.ali_user_id, new_start_at=L(D(1), "12:00")
        )
    async with write_tx(w.sm) as s:
        out = await bookings.expire_due_pending(s, now=p.booking.expire_at + timedelta(seconds=1))
    assert out and out[0].status == BookingStatus.EXPIRED
    await H.notifier.booking_expired(out[0])
    async with read_tx(w.sm) as s:
        tg = (await users.get_user(s, c)).tg_id
    texts = H.texts_to(tg)
    assert texts, "client got no message"
    assert any("javob bermadingiz" in x for x in texts)  # notify.client_proposal_expired
    assert not any("o'z vaqtida javob bera olmadi" in x for x in texts)  # notify.client_expired


def test_allowed_updates_include_my_chat_member(world: World) -> None:
    from app.bot.main import allowed_updates
    from tests.bot_harness import Harness

    types = allowed_updates(Harness(world.sm).dp)
    assert "my_chat_member" in types and "callback_query" in types


def test_clip_keeps_lines_whole() -> None:
    from app.bot.screen import clip

    text = "\n".join(f"<b>line {i}</b>" for i in range(1000))
    out = clip(text)
    assert len(out) < 4096 and out.endswith("…")
    assert out.count("<b>") == out.count("</b>")


async def test_map_url_validated(world: World) -> None:
    from app.services.errors import InputError

    with pytest.raises(InputError):
        await world.set("map_google_url", "https://maps google.com/x")
    with pytest.raises(InputError):
        await world.set("map_google_url", "http://")
    await world.set("map_google_url", "https://maps.google.com/?q=1")
    await world.set("map_google_url", "")


async def test_huge_bonus_delta_rejected(world: World) -> None:
    from app.services import bonus
    from app.services.errors import InputError

    c = await world.new_client()
    with pytest.raises(InputError):
        async with write_tx(world.sm) as s:
            await bonus.adjust_bonus(
                s, actor_user_id=world.admin_id, user_id=c, delta=10**20, comment="x"
            )


def test_time_off_reason_is_translated() -> None:
    from app.bot.views import reason_label

    for lang in ("uz", "ru"):
        assert reason_label(lang, "time_off") not in ("", "time_off")
        assert reason_label(lang, "barber_inactive") not in ("", "barber_inactive")
