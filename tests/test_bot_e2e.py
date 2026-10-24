"""Bot handlers driven through the real Dispatcher stack with a fake Telegram session."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.bot.callbacks import (
    BarberCb,
    BookingCb,
    ConsentCb,
    DayCb,
    LangCb,
    SlotCb,
    SvcCb,
    WizCb,
)
from app.bot.i18n import t
from app.db.enums import BookingStatus
from app.db.models import Booking
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, slots, users
from tests.bot_harness import Harness
from tests.factories import World, book


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def _first_free(w: World, *, barber_id: int, service_id: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        days = await slots.get_available_days(s, barber_id=barber_id, service_id=service_id)
        for d in days:
            if d.has_free_slots:
                free = await slots.get_free_slots(
                    s, barber_id=barber_id, service_id=service_id, day=d.day
                )
                return d.day, free
    raise AssertionError("no free day")


@pytest.fixture
def H(world: World) -> Harness:  # noqa: N802
    return Harness(world.sm)


async def test_onboarding_language_and_consent_gate(world: World, H: Harness) -> None:  # noqa: N803
    tg = 880001
    await H.text(tg, "/start", lang="en")
    assert LangCb(l="uz").pack() in H.last_callbacks(tg)
    await H.tap(tg, LangCb(l="uz").pack(), message_id=1)
    assert ConsentCb(a="ok").pack() in H.last_callbacks(tg)
    # any menu button before consent re-shows the consent screen
    n = len(H.session.calls)
    await H.text(tg, t("uz", "menu.book"))
    assert ConsentCb(a="ok").pack() in H.last_callbacks(tg)
    assert len(H.session.calls) > n
    async with read_tx(world.sm) as s:
        ctx = await users.load_context(s, tg_id=tg)
    assert ctx is not None and ctx.user.lang_chosen and ctx.user.consent_version is None
    await H.tap(tg, ConsentCb(a="ok").pack(), message_id=1)
    async with read_tx(world.sm) as s:
        ctx = await users.load_context(s, tg_id=tg)
    assert ctx.user.consent_version is not None


async def test_russian_skips_picker(world: World, H: Harness) -> None:  # noqa: N803
    tg = 880002
    await H.text(tg, "/start", lang="ru")
    cbs = H.last_callbacks(tg)
    assert LangCb(l="uz").pack() not in cbs and ConsentCb(a="ok").pack() in cbs


async def test_whoami(world: World, H: Harness) -> None:  # noqa: N803
    await H.text(1000, "/whoami")
    out = "\n".join(H.texts_to(1000))
    assert "1000" in out


async def test_booking_wizard_end_to_end(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client(name="Valijon")
    tg = await _tg(world, c)
    day, free = await _first_free(world, barber_id=world.ali_id, service_id=world.svc30)
    start = free[0]
    loc = clock.to_local(start)

    await H.text(tg, t("uz", "menu.book"))
    assert SvcCb(s=world.svc30).pack() in H.last_callbacks(tg)
    await H.tap(tg, SvcCb(s=world.svc30).pack())
    assert BarberCb(b=world.ali_id).pack() in H.last_callbacks(tg)
    await H.tap(tg, BarberCb(b=world.ali_id).pack())
    dcode = day.strftime("%Y%m%d")
    assert DayCb(d=dcode).pack() in H.last_callbacks(tg)
    await H.tap(tg, DayCb(d=dcode).pack())
    slot_cb = SlotCb(b=world.ali_id, s=world.svc30, d=dcode, t=loc.strftime("%H%M")).pack()
    assert slot_cb in H.last_callbacks(tg)
    # a tap on an old message is STALE [E2]
    await H.tap(tg, slot_cb, message_id=99999)
    assert t("uz", "common.stale") in H.alerts()
    await H.tap(tg, slot_cb)
    assert WizCb(a="skip_comment").pack() in H.last_callbacks(tg)
    await H.text(tg, "<b>soqol</b>")
    assert WizCb(a="confirm").pack() in H.last_callbacks(tg)
    # back keeps choices and goes to the comment step
    await H.tap(tg, WizCb(a="back").pack())
    assert WizCb(a="skip_comment").pack() in H.last_callbacks(tg)
    await H.tap(tg, WizCb(a="skip_comment").pack())
    await H.tap(tg, WizCb(a="confirm").pack())

    async with read_tx(world.sm) as s:
        rows = (await s.execute(select(Booking))).scalars().all()
        assert len(rows) == 1
        b = rows[0]
        assert b.start_at == start and b.client_id == c and b.status == BookingStatus.CONFIRMED
        assert b.comment is None  # skipped after going back
    assert await H.fsm_state(tg) is None
    # double tap on confirm after the wizard ended creates nothing [#10]
    await H.tap(tg, WizCb(a="confirm").pack(), message_id=1)
    async with read_tx(world.sm) as s:
        assert len((await s.execute(select(Booking))).scalars().all()) == 1


async def test_contact_step_and_foreign_contact(world: World, H: Harness) -> None:  # noqa: N803
    from tests.factories import new_client

    c = await new_client(world.sm, verified=False, name="Nodir")
    tg = await _tg(world, c)
    day, free = await _first_free(world, barber_id=world.ali_id, service_id=world.svc30)
    loc = clock.to_local(free[0])
    await H.text(tg, t("uz", "menu.book"))
    await H.tap(tg, SvcCb(s=world.svc30).pack())
    await H.tap(tg, BarberCb(b=world.ali_id).pack())
    dcode = day.strftime("%Y%m%d")
    await H.tap(tg, DayCb(d=dcode).pack())
    await H.tap(tg, SlotCb(b=world.ali_id, s=world.svc30, d=dcode, t=loc.strftime("%H%M")).pack())
    old_msg = await H.fsm_msg_id(tg)
    await H.tap(tg, WizCb(a="skip_comment").pack())
    assert await H.fsm_state(tg) == "BookingFlow:contact"
    await H.text(tg, "90 123 45 67")
    assert t("uz", "contact.use_button") in H.texts_to(tg)
    await H.contact(tg, "+998901234567", owner=tg + 1)  # someone else's contact
    assert t("uz", "contact.only_own") in H.texts_to(tg)
    await H.contact(tg, "998901234567")
    assert await H.fsm_state(tg) == "BookingFlow:confirm"
    new_msg = await H.fsm_msg_id(tg)
    assert new_msg != old_msg
    async with read_tx(world.sm) as s:
        u = await users.get_user(s, c)
    assert u.phone == "+998901234567" and u.phone_verified
    await H.tap(tg, WizCb(a="confirm").pack())
    async with read_tx(world.sm) as s:
        assert len((await s.execute(select(Booking))).scalars().all()) == 1


async def test_barber_accepts_and_client_is_told(world: World, H: Harness) -> None:  # noqa: N803
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    tg = await _tg(world, c)
    day, free = await _first_free(world, barber_id=world.ali_id, service_id=world.svc30)
    r = await book(world, c, free[-1], now=clock.now_utc())
    assert r.booking.status == BookingStatus.PENDING
    await H.tap(2000, BookingCb(a="acc", id=r.booking.id).pack(), message_id=5)
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.CONFIRMED
    assert H.texts_to(tg), "client got no confirmation"


async def test_crafted_callback_from_other_barber(world: World, H: Harness) -> None:  # noqa: N803
    _, bek_user = await world.new_barber("Bekzod")
    bek_tg = await _tg(world, bek_user)
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    day, free = await _first_free(world, barber_id=world.ali_id, service_id=world.svc30)
    r = await book(world, c, free[-1], now=clock.now_utc())
    for a in ("acc", "card", "rej", "done", "ns"):
        await H.tap(bek_tg, BookingCb(a=a, id=r.booking.id).pack(), message_id=5)
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.PENDING
    assert H.alerts(), "expected a forbidden alert"
    # and nothing about the booking leaked to Bekzod
    client_name = r.booking.client_name
    assert not any(client_name in x for x in H.texts_to(bek_tg))


async def test_admin_callback_refused_for_client(world: World, H: Harness) -> None:  # noqa: N803
    from app.bot.callbacks import AdminCb

    c = await world.new_client()
    tg = await _tg(world, c)
    n_before = len(H.session.calls)
    await H.tap(tg, AdminCb(s="br", a="list").pack(), message_id=5)
    sent = [type(x).__name__ for x in H.session.calls[n_before:]]
    # no admin screen: at most an answer/alert or a fallback message
    assert "EditMessageText" not in sent or all(
        "Ali" not in (getattr(x, "text", "") or "") for x in H.session.calls[n_before:]
    )


async def test_client_cancels_via_my_bookings(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    day, free = await _first_free(world, barber_id=world.ali_id, service_id=world.svc30)
    r = await book(world, c, free[-1], now=clock.now_utc())
    await H.text(tg, t("uz", "menu.my"))
    assert BookingCb(a="ccx", id=r.booking.id).pack() in H.last_callbacks(tg)
    await H.tap(tg, BookingCb(a="ccx", id=r.booking.id).pack())
    cbs = H.last_callbacks(tg)
    if BookingCb(a="ccy", id=r.booking.id).pack() in cbs:
        await H.tap(tg, BookingCb(a="ccy", id=r.booking.id).pack())
    async with read_tx(world.sm) as s:
        b = await bookings.get_booking(s, r.booking.id)
    assert b.status == BookingStatus.CANCELLED_BY_CLIENT


async def test_blocked_user_dropped(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    async with write_tx(world.sm) as s:
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=c, blocked=True)
    await H.text(tg, t("uz", "menu.book"))
    await H.text(tg, t("uz", "menu.book"))
    blocked = [x for x in H.texts_to(tg) if x == t("uz", "common.blocked")]
    assert len(blocked) == 1  # at most one notice per 10 minutes
    assert await H.fsm_state(tg) is None


async def test_unreachable_barber_recovers_on_any_message(world: World, H: Harness) -> None:  # noqa: N803
    from app.services import barbers

    async with write_tx(world.sm) as s:
        await barbers.set_reachable(s, barber_id=world.ali_id, reachable=False)
    await H.text(2000, "/start")
    async with read_tx(world.sm) as s:
        assert (await barbers.get_barber(s, world.ali_id)).is_reachable


async def test_today_for_barber(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client(phone="+998901234567")
    async with read_tx(world.sm) as s:
        free = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=clock.local_today()
        )
    if not free:
        pytest.skip("no slots left today in real time")
    await book(world, c, free[0], now=clock.now_utc())
    await H.text(2000, "/today")
    assert BookingCb(a="card", id=1).pack() in H.last_callbacks(2000)
    # phone shown as text in the list
    assert any("+998901234567" in x for x in H.texts_to(2000))
    assert timedelta


async def test_throttle_messages_get_one_notice(world: World) -> None:
    H = Harness(world.sm)  # noqa: N806
    from app.bot.middlewares import ThrottleMiddleware

    thr = next(m for m in H.dp.update.outer_middleware if isinstance(m, ThrottleMiddleware))
    thr.limit = 5
    c = await world.new_client()
    tg = await _tg(world, c)
    for _ in range(12):
        await H.text(tg, "hello")
    assert H.texts_to(tg).count(t("uz", "common.too_fast")) == 1


async def test_group_messages_ignored_and_bot_leaves(world: World, H: Harness) -> None:  # noqa: N803
    from datetime import datetime, timezone

    from aiogram.types import (
        Chat,
        ChatMemberLeft,
        ChatMemberMember,
        ChatMemberUpdated,
        Message,
        Update,
    )
    from aiogram.types import User as TgUser

    grp = Chat(id=-100, type="group", title="g")
    u = TgUser(id=555, is_bot=False, first_name="G")
    msg = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=grp, from_user=u, text="/start"
    )
    await H.dp.feed_update(H.bot, Update(update_id=90001, message=msg))
    me = TgUser(id=42, is_bot=True, first_name="Bot")
    upd = ChatMemberUpdated(
        chat=grp,
        from_user=u,
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(user=me),
        new_chat_member=ChatMemberMember(user=me),
    )
    await H.dp.feed_update(H.bot, Update(update_id=90002, my_chat_member=upd))
    names = [type(c).__name__ for c in H.session.calls]
    assert "SendMessage" not in names and "LeaveChat" in names
    async with read_tx(world.sm) as s:
        assert await users.load_context(s, tg_id=555) is None
