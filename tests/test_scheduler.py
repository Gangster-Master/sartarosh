"""The tick: expiry, auto-complete, outbox delivery and once-a-day jobs."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.bot.callbacks import BookingCb
from app.bot.i18n import t
from app.bot.scheduler import Ticker
from app.db.enums import BookingStatus
from app.db.enums import NotificationKind as K
from app.db.models import ScheduledNotification
from app.db.uow import read_tx
from app.services import bookings, clock, settings, users
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World, book


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start: datetime) -> None:
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


@pytest.fixture
def H(world: World) -> Harness:  # noqa: N802
    return Harness(world.sm)


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def test_reminder_24h_delivered_once_with_buttons(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    start = L(D(1), "10:00")
    r = await book(world, c, start)
    clk = Clock(monkeypatch, start - timedelta(hours=24))
    ticker = Ticker(H.bot, world.sm, H.notifier)
    await ticker.tick()
    msgs = H.sent_to(tg)
    assert len(msgs) == 1
    cbs = [b.callback_data for row in msgs[0].reply_markup.inline_keyboard for b in row]
    assert BookingCb(a="ack", id=r.booking.id).pack() in cbs
    assert BookingCb(a="ccx", id=r.booking.id).pack() in cbs
    clk.now += timedelta(minutes=1)
    await ticker.tick()
    assert len(H.sent_to(tg)) == 1
    async with read_tx(world.sm) as s:
        row = (
            await s.execute(
                select(ScheduledNotification).where(ScheduledNotification.kind == K.REMINDER_24H)
            )
        ).scalar_one()
        assert row.sent_at is not None


async def test_kelaman_suppresses_2h(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    tg = await _tg(world, c)
    start = L(D(1), "10:00")
    r = await book(world, c, start)
    clk = Clock(monkeypatch, start - timedelta(hours=24))
    ticker = Ticker(H.bot, world.sm, H.notifier)
    await ticker.tick()
    await H.tap(tg, BookingCb(a="ack", id=r.booking.id).pack(), message_id=5)
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).client_ack_at is not None
    n = len(H.sent_to(tg))
    clk.now = start - timedelta(hours=2)
    await ticker.tick()
    assert len(H.sent_to(tg)) == n  # no 2 h reminder after "Kelaman"
    assert t


async def test_expire_tick_tells_client(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client()
    tg = await _tg(world, c)
    created = L(D(0), "11:00")
    r = await book(world, c, L(D(0), "15:00"), now=created)
    Clock(monkeypatch, L(D(0), "12:00"))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, r.booking.id)).status == BookingStatus.EXPIRED
    assert H.sent_to(tg), "client was not told about the expiry"


async def test_auto_complete_tick(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "10:00"))
    Clock(monkeypatch, L(D(0), "22:31"))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    async with read_tx(world.sm) as s:
        b = await bookings.get_booking(s, r.booking.id)
    assert b.status == BookingStatus.COMPLETED and b.auto_completed


async def test_morning_summary_once_and_not_after_grace(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    await book(world, c, L(D(1), "10:00"))
    clk = Clock(monkeypatch, L(D(1), "08:00"))
    ticker = Ticker(H.bot, world.sm, H.notifier)
    await ticker.tick()
    first = len(H.sent_to(2000))
    assert first == 1
    clk.now = L(D(1), "08:30")  # "restart" at 08:30: the marker stops a resend
    await Ticker(H.bot, world.sm, H.notifier).tick()
    assert len(H.sent_to(2000)) == first
    async with read_tx(world.sm) as s:
        assert await settings.get_marker(s, "_job.morning") == D(1).isoformat()
    # next day the bot was down until 11:30: summary skipped (grace 3h)
    clk.now = L(D(2), "11:30")
    await ticker.tick()
    assert len(H.sent_to(2000)) == first


async def test_nightly_digest_lists_unresolved(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client()
    r = await book(world, c, L(D(0), "18:00"))
    Clock(monkeypatch, L(D(0), "23:00"))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    msgs = [m for m in H.sent_to(2000) if m.reply_markup is not None]
    cbs = [b.callback_data for m in msgs for row in m.reply_markup.inline_keyboard for b in row]
    assert BookingCb(a="dd", id=r.booking.id).pack() in cbs
    assert BookingCb(a="dn", id=r.booking.id).pack() in cbs
    assert NOW


async def test_digest_button_keeps_the_rest_of_the_list(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c1 = await world.new_client()
    c2 = await world.new_client(phone="+998907776655")
    r1 = await book(world, c1, L(D(0), "17:00"))
    r2 = await book(world, c2, L(D(0), "18:00"))
    Clock(monkeypatch, L(D(0), "23:00"))
    await Ticker(H.bot, world.sm, H.notifier).tick()
    digest = [m for m in H.sent_to(2000) if m.reply_markup is not None][-1]
    n_calls = len(H.session.calls)
    await H.tap(
        2000,
        BookingCb(a="dd", id=r1.booking.id).pack(),
        message_id=777,
        reply_markup=digest.reply_markup,
    )
    new = H.session.calls[n_calls:]
    names = [type(c).__name__ for c in new]
    assert "EditMessageText" not in names
    edits = [c for c in new if type(c).__name__ == "EditMessageReplyMarkup"]
    assert edits
    left = [b.callback_data for row in edits[-1].reply_markup.inline_keyboard for b in row]
    assert BookingCb(a="dd", id=r2.booking.id).pack() in left
    assert all(BookingCb.unpack(x).id != r1.booking.id for x in left)
    from app.db.enums import BookingStatus
    from app.db.uow import read_tx

    async with read_tx(world.sm) as s:
        b1 = await bookings.load_booking(s, r1.booking.id)
        assert b1.status == BookingStatus.COMPLETED


async def test_mcf_text_names_the_day_when_sent_later(
    world: World,
    H: Harness,
    monkeypatch: pytest.MonkeyPatch,  # noqa: N803
) -> None:
    c = await world.new_client(phone="+998901234567")
    tg = await _tg(world, c)
    from app.db.uow import write_tx

    async with write_tx(world.sm) as s:
        await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=L(D(0), "15:00"),
            client_name="Walk In",
            client_phone="+998901234567",
            now=NOW,
        )
    clk = Clock(monkeypatch, L(D(1), "03:30"))
    ticker = Ticker(H.bot, world.sm, H.notifier)
    await ticker.tick()  # auto-completes; mcf waits for daytime
    assert H.sent_to(tg) == []
    clk.now = L(D(1), "10:00")
    await ticker.tick()
    texts = H.texts_to(tg)
    assert len(texts) == 1 and "26-oktabr" in texts[0] and "Bugun" not in texts[0]
    cbs = [b.callback_data for row in H.sent_to(tg)[0].reply_markup.inline_keyboard for b in row]
    assert any(x.startswith("bk:vy:") for x in cbs)
