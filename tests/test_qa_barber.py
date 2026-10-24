"""QA regression: barber journeys (late accept, time off, self-booking, walk-ins, summary, unreachable).

Each test asserts the correct behaviour; each was a confirmed bug, now fixed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from app.bot.callbacks import BookingCb
from app.bot.i18n import t
from app.bot.scheduler import Ticker
from app.db.enums import Awaiting, BookingStatus, NotificationKind
from app.db.models import ScheduledNotification
from app.db.uow import read_tx, write_tx
from app.services import barbers, bookings, clock, reviews, schedule, users
from app.services.errors import ServiceError
from tests.bot_harness import FakeSession, Harness
from tests.factories import NOW, D, L, World, book


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start) -> None:  # type: ignore[no-untyped-def]
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest.fixture
def H(world: World, clk: Clock) -> Harness:  # noqa: N802
    return Harness(world.sm)


async def _get(w: World, bid: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        return await bookings.get_booking(s, bid)


async def _pending(world: World) -> Any:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client(name="Mijoz")
    r = await book(world, c, L(D(1), "10:30"))
    assert r.booking.status == BookingStatus.PENDING and r.booking.awaiting == Awaiting.BARBER
    return r


# --------------------------------------------------------------------------- 1. late accept


async def test_late_accept_is_reachable_in_bot_within_grace(
    world: World,
    H: Harness,  # noqa: N803
    clk: Clock,
) -> None:
    """REQUIREMENTS B2/[C-B5]: a tap up to 10 min after expiry still confirms. In the bot the
    expiry edit strips the decision buttons and the card of an EXPIRED booking has no accept
    button, so the barber has no way to late-accept (the Mini App does offer it)."""
    r = await _pending(world)
    bid = r.booking.id
    await H.notifier.booking_created(r)  # decision message to Ali (tg 2000)
    expire_at = r.booking.expire_at
    clk.now = expire_at + timedelta(minutes=1)
    await Ticker(H.bot, world.sm, H.notifier)._expire()
    assert (await _get(world, bid)).status == BookingStatus.EXPIRED
    clk.now = expire_at + timedelta(minutes=3)  # still inside late_accept_grace_min (10)
    # the decision message edited at expiry: does it still offer accept?
    edited_has_acc = BookingCb(a="acc", id=bid).pack() in H.last_callbacks(2000)
    await H.tap(2000, BookingCb(a="card", id=bid).pack(), message_id=5)
    card_has_acc = BookingCb(a="acc", id=bid).pack() in H.last_callbacks(2000)
    assert edited_has_acc or card_has_acc, "no accept button anywhere in the bot within grace"


# --------------------------------------------------------------------------- 2. late accept re-check


async def test_late_accept_refused_inside_shop_day_off(world: World) -> None:
    """Late accept only re-checks booking cells; a day off (time off) added after the expiry
    is ignored, so the booking is confirmed on a closed day."""
    r = await _pending(world)
    bid = r.booking.id
    exp = r.booking.expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    async with write_tx(world.sm) as s:
        iv = schedule.full_day_interval(D(1))
        await schedule.add_time_off(
            s,
            actor_user_id=world.admin_id,
            barber_id=None,
            start_at=iv.start,
            end_at=iv.end,
            reason="Bayram",
            cancel_conflicts=True,
            now=exp + timedelta(minutes=1),
        )
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.ali_user_id, now=exp + timedelta(minutes=5)
            )
    assert (await _get(world, bid)).status != BookingStatus.CONFIRMED


async def test_late_accept_refused_for_deactivated_barber(world: World) -> None:
    """Deactivation cancels only ACTIVE bookings; an EXPIRED one inside the grace can still be
    late-accepted by an admin, giving an inactive barber a confirmed booking."""
    r = await _pending(world)
    bid = r.booking.id
    exp = r.booking.expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    async with write_tx(world.sm) as s:
        await barbers.deactivate_barber(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            reason="ketdi",
            now=exp + timedelta(minutes=1),
        )
    with pytest.raises(ServiceError):
        async with write_tx(world.sm) as s:
            await bookings.confirm_booking(
                s, booking_id=bid, actor_user_id=world.admin_id, now=exp + timedelta(minutes=5)
            )
    assert (await _get(world, bid)).status != BookingStatus.CONFIRMED


# --------------------------------------------------------------------------- 3. self-booking / rating


async def test_barber_cannot_rate_himself_via_self_booking(world: World) -> None:
    """A barber books himself as a client, marks it done and rates it 5 stars: the review
    counts in his public rating (and he earns stamps)."""
    async with write_tx(world.sm) as s:
        await users.set_verified_contact(s, user_id=world.ali_user_id, raw_phone="+998901112233")
    rated = False
    try:
        r = await book(world, world.ali_user_id, L(D(0), "11:00"))
        async with write_tx(world.sm) as s:
            await bookings.complete_booking(
                s,
                booking_id=r.booking.id,
                actor_user_id=world.ali_user_id,
                now=L(D(0), "11:30"),
            )
        async with write_tx(world.sm) as s:
            await reviews.submit_rating(
                s, booking_id=r.booking.id, client_user_id=world.ali_user_id, rating=5
            )
        rated = True
    except ServiceError:
        pass
    async with read_tx(world.sm) as s:
        ali = await barbers.get_barber(s, world.ali_id)
    assert not rated and ali.rating_count == 0, "barber rated his own visit"


async def test_walkin_with_barbers_own_phone_is_not_linked_to_him(world: World) -> None:
    """A walk-in typed with the barber's own verified phone is linked to the barber himself;
    he then gets the 'edingizmi?' prompt, can answer 'Ha' and farm stamps (+ review prompt)."""
    async with write_tx(world.sm) as s:
        await users.set_verified_contact(s, user_id=world.ali_user_id, raw_phone="+998901112233")
    async with write_tx(world.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=L(D(0), "10:00"),
            client_name="Men",
            client_phone="+998901112233",
            now=NOW,
        )
    assert b.client_id != world.ali_user_id


# --------------------------------------------------------------------------- 4. walk-in pushes


async def test_completed_walkin_schedules_only_visit_check(world: World) -> None:
    """REQUIREMENTS B9: no client pushes for walk-ins except the visit check C19. Completing a
    linked walk-in also schedules RETURN ('time for a haircut') and REVIEW pushes before the
    client has confirmed the visit (and they survive a 'Yo'q' answer)."""
    c = await world.new_client(phone="+998901234567")
    async with write_tx(world.sm) as s:
        b = await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=L(D(0), "10:00"),
            client_name="Walk",
            client_phone="+998901234567",
            now=NOW,
        )
    assert b.client_id == c
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=b.id, actor_user_id=world.ali_user_id, now=L(D(0), "10:30")
        )
    async with write_tx(world.sm) as s:
        await bookings.confirm_manual_visit(
            s, booking_id=b.id, client_user_id=c, yes=False, now=L(D(0), "10:40")
        )
    async with read_tx(world.sm) as s:
        kinds = set(
            (
                await s.execute(
                    select(ScheduledNotification.kind).where(
                        ScheduledNotification.booking_id == b.id,
                        ScheduledNotification.cancelled_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert kinds <= {NotificationKind.MANUAL_VISIT_CONFIRM}, kinds


# --------------------------------------------------------------------------- 5. morning summary


async def test_morning_summary_tells_barber_with_empty_day(
    world: World,
    H: Harness,  # noqa: N803
    clk: Clock,
) -> None:
    """notify.morning_empty exists and morning_summary has a branch for it, but
    bookings_for_daily_summary only returns barbers WITH bookings, so a barber with an empty
    day never gets the 08:00 message (dead branch)."""
    clk.now = L(D(0), "08:01")
    await Ticker(H.bot, world.sm, H.notifier)._daily()
    assert t("uz", "notify.morning_empty") in H.texts_to(2000)


# --------------------------------------------------------------------------- 6. unreachable spam


class _BlockedBarberSession(FakeSession):
    def __init__(self, blocked: int) -> None:
        super().__init__()
        self.blocked = blocked

    async def make_request(self, bot, method, timeout=None):  # type: ignore[no-untyped-def]
        if getattr(method, "chat_id", None) == self.blocked:
            self.calls.append(method)
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        return await super().make_request(bot, method, timeout)


async def test_unreachable_barber_alert_sent_once(world: World, clk: Clock) -> None:  # noqa: ARG001
    """Each failed send to a barber who blocked the bot calls _barber_unreachable, which alerts
    every admin again even when the barber is already marked unreachable."""
    h = Harness(world.sm)
    h.session = _BlockedBarberSession(2000)
    h.bot._session = h.session  # type: ignore[attr-defined]
    h.bot.session = h.session
    h.notifier.bot = h.bot
    c1, c2 = await world.new_client(), await world.new_client()
    r1 = await book(world, c1, L(D(1), "10:00"))
    r2 = await book(world, c2, L(D(1), "11:00"))
    for r, cid in ((r1, c1), (r2, c2)):
        async with write_tx(world.sm) as s:
            res = await bookings.cancel_by_client(
                s, booking_id=r.booking.id, client_user_id=cid, now=NOW
            )
        await h.notifier.booking_cancelled(res.booking, by_client=True)
    async with read_tx(world.sm) as s:
        ali = await barbers.get_barber(s, world.ali_id)
    assert not ali.is_reachable
    alert = t("uz", "admin.alert.barber_unreachable", name="Ali")
    n = sum(1 for x in h.texts_to(1000) if x == alert)
    assert n == 1, f"admins alerted {n} times"


# --------------------------------------------------------------------------- 7. proposal overlap


async def test_proposal_cannot_give_client_overlapping_bookings(world: World) -> None:
    """create_booking and late accept refuse a client overlap, but propose_time/answer_proposal
    don't check the client's other bookings: the barber proposes a time at which the client
    already has a booking with another barber, the client accepts, and now holds two
    overlapping ACTIVE bookings."""
    bek, _ = await world.new_barber("Bekzod")
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client(name="Mijoz")
    r_ali = await book(world, c, L(D(1), "10:30"))
    r_bek = await book(world, c, L(D(1), "11:00"), barber_id=bek)
    assert r_ali.booking.status == BookingStatus.PENDING
    try:
        async with write_tx(world.sm) as s:
            await bookings.propose_time(
                s,
                booking_id=r_ali.booking.id,
                actor_user_id=world.ali_user_id,
                new_start_at=L(D(1), "11:00"),
                now=NOW,
            )
        async with write_tx(world.sm) as s:
            await bookings.answer_proposal(
                s,
                booking_id=r_ali.booking.id,
                client_user_id=c,
                accept=True,
                now=NOW + timedelta(minutes=5),
            )
    except ServiceError:
        pass
    a, b = await _get(world, r_ali.booking.id), await _get(world, r_bek.booking.id)
    active = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
    both_active = a.status in active and b.status in active
    overlap = a.start_at < b.end_at and b.start_at < a.end_at
    assert not (both_active and overlap), "client holds two overlapping bookings"
