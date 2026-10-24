"""Client and barber flows through the bot with a pinned clock (NOW = Mon 09:00)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.bot.callbacks import (
    BarberCb,
    BookingCb,
    DayCb,
    HubCb,
    ProposeCb,
    RebookCb,
    RejectCb,
    SlotCb,
    SvcCb,
    WizCb,
)
from app.bot.i18n import t
from app.db.enums import Awaiting, BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, users
from tests.bot_harness import Harness
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


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def _get(w: World, bid: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        return await bookings.get_booking(s, bid)


async def test_any_barber_flow_assigns_least_loaded(world: World, H: Harness) -> None:  # noqa: N803
    bek, bek_user = await world.new_barber("Bekzod")
    other = await world.new_client()
    await book(world, other, L(D(1), "15:00"))  # Ali busier on Tuesday
    c = await world.new_client()
    tg = await _tg(world, c)
    await H.text(tg, t("uz", "menu.book"))
    await H.tap(tg, SvcCb(s=world.svc30).pack())
    assert BarberCb(b=0).pack() in H.last_callbacks(tg)
    await H.tap(tg, BarberCb(b=0).pack())
    await H.tap(tg, DayCb(d=D(1).strftime("%Y%m%d")).pack())
    slot = SlotCb(b=0, s=world.svc30, d=D(1).strftime("%Y%m%d"), t="1000").pack()
    assert slot in H.last_callbacks(tg)
    await H.tap(tg, slot)
    await H.tap(tg, WizCb(a="skip_comment").pack())
    await H.tap(tg, WizCb(a="confirm").pack())
    async with read_tx(world.sm) as s:
        res = await bookings.list_client_bookings(s, client_user_id=c)
    assert len(res.upcoming) == 1 and res.upcoming[0].barber_id == bek
    assert any("Bekzod" in x for x in H.texts_to(tg))


async def test_rebook_from_my_list_reaches_day_step(world: World, H: Harness, clk: Clock) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    r = await book(world, c, L(D(0), "10:00"), service_id="60")
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=r.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "11:00")
        )
    clk.now = L(D(0), "12:00")
    await H.text(tg, t("uz", "menu.my"))
    rb = RebookCb(b=world.ali_id, s=world.svc60).pack()
    assert rb in H.last_callbacks(tg)
    await H.tap(tg, rb, message_id=7)
    assert await H.fsm_state(tg) == "BookingFlow:day"  # 1 tap to the day step [#15]
    data = await H.storage.get_data(H.key(tg))
    assert data["barber_id"] == world.ali_id and data["service_id"] == world.svc60


async def test_wizard_cancel_and_session_expired(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    await H.text(tg, t("uz", "menu.book"))
    mid = await H.fsm_msg_id(tg)
    await H.tap(tg, WizCb(a="cancel").pack())
    assert await H.fsm_state(tg) is None
    await H.tap(tg, SvcCb(s=world.svc30).pack(), message_id=mid)
    assert t("uz", "common.session_expired") in H.alerts()


async def test_menu_button_leaves_wizard(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    await H.text(tg, t("uz", "menu.book"))
    assert (await H.fsm_state(tg) or "").startswith("BookingFlow")
    await H.text(tg, t("uz", "menu.my"))
    assert await H.fsm_state(tg) is None
    await H.text(tg, t("uz", "menu.book"))
    await H.text(tg, "/cancel")
    assert await H.fsm_state(tg) is None


async def test_edit_name_on_summary(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client(name="Valijon")
    tg = await _tg(world, c)
    await H.text(tg, t("uz", "menu.book"))
    await H.tap(tg, SvcCb(s=world.svc30).pack())
    await H.tap(tg, BarberCb(b=world.ali_id).pack())
    await H.tap(tg, DayCb(d=D(1).strftime("%Y%m%d")).pack())
    await H.tap(
        tg, SlotCb(b=world.ali_id, s=world.svc30, d=D(1).strftime("%Y%m%d"), t="1030").pack()
    )
    await H.tap(tg, WizCb(a="skip_comment").pack())
    summary = [x for x in H.texts_to(tg) if "26" in x or "27" in x]
    assert any("27-oktabr, seshanba, 10:30" in x for x in summary)
    await H.tap(tg, WizCb(a="edit_name").pack())
    await H.text(tg, "A")
    assert await H.fsm_state(tg) == "BookingFlow:name_input"
    await H.text(tg, "<b>x</b>")
    assert await H.fsm_state(tg) == "BookingFlow:name_input"
    await H.text(tg, "Gʻayrat")
    assert await H.fsm_state(tg) == "BookingFlow:confirm"
    await H.tap(tg, WizCb(a="confirm").pack())
    async with read_tx(world.sm) as s:
        res = await bookings.list_client_bookings(s, client_user_id=c)
    assert res.upcoming[0].client_name == "Gʻayrat"


async def test_slot_taken_meanwhile_returns_to_grid(world: World, H: Harness) -> None:  # noqa: N803
    c, c2 = await world.new_client(), await world.new_client()
    tg = await _tg(world, c)
    await H.text(tg, t("uz", "menu.book"))
    await H.tap(tg, SvcCb(s=world.svc30).pack())
    await H.tap(tg, BarberCb(b=world.ali_id).pack())
    await H.tap(tg, DayCb(d=D(1).strftime("%Y%m%d")).pack())
    await H.tap(
        tg, SlotCb(b=world.ali_id, s=world.svc30, d=D(1).strftime("%Y%m%d"), t="1030").pack()
    )
    await H.tap(tg, WizCb(a="skip_comment").pack())
    await book(world, c2, L(D(1), "10:30"))  # someone else was faster
    await H.tap(tg, WizCb(a="confirm").pack())
    assert t("uz", "err.slot_taken") in H.alerts() or any("slot" in a for a in H.alerts())
    assert await H.fsm_state(tg) == "BookingFlow:slot"
    taken = SlotCb(b=world.ali_id, s=world.svc30, d=D(1).strftime("%Y%m%d"), t="1030").pack()
    assert taken not in H.last_callbacks(tg)


async def _pending(world: World) -> tuple[int, int, int]:
    await world.set("new_client_requires_approval", "true")
    c = await world.new_client(name="Mijoz")
    r = await book(world, c, L(D(1), "10:30"))
    return r.booking.id, c, await _tg(world, c)


async def test_reject_with_free_text_reason(world: World, H: Harness) -> None:  # noqa: N803
    bid, c, tg = await _pending(world)
    await H.tap(2000, BookingCb(a="rej", id=bid).pack(), message_id=5)
    assert RejectCb(id=bid, r="other").pack() in H.last_callbacks(2000)
    await H.tap(2000, RejectCb(id=bid, r="other").pack(), message_id=5)
    await H.text(2000, "<b>Kasalman</b>")
    b = await _get(world, bid)
    assert b.status == BookingStatus.REJECTED and b.cancel_reason == "<b>Kasalman</b>"
    told = "\n".join(H.texts_to(tg))
    assert "&lt;b&gt;Kasalman&lt;/b&gt;" in told and "<b>Kasalman</b>" not in told
    assert any(x.startswith("rb:") for x in H.last_callbacks(tg))


async def test_propose_and_client_accepts(world: World, H: Harness) -> None:  # noqa: N803
    bid, c, tg = await _pending(world)
    await H.tap(2000, BookingCb(a="prop", id=bid).pack(), message_id=5)
    opts = [x for x in H.last_callbacks(2000) if x.startswith("pp:")]
    assert 0 < len(opts) <= 8
    pick = ProposeCb(id=bid, d=D(1).strftime("%Y%m%d"), t="1100").pack()
    assert pick in opts
    await H.tap(2000, pick, message_id=5)
    b = await _get(world, bid)
    assert b.awaiting == Awaiting.CLIENT and b.start_at == L(D(1), "11:00")
    assert BookingCb(a="pya", id=bid).pack() in H.last_callbacks(tg)
    await H.tap(tg, BookingCb(a="pya", id=bid).pack(), message_id=9)
    b = await _get(world, bid)
    assert b.status == BookingStatus.CONFIRMED and b.start_at == L(D(1), "11:00")


async def test_propose_and_client_declines(world: World, H: Harness) -> None:  # noqa: N803
    bid, c, tg = await _pending(world)
    await H.tap(2000, BookingCb(a="prop", id=bid).pack(), message_id=5)
    await H.tap(2000, ProposeCb(id=bid, d=D(1).strftime("%Y%m%d"), t="1100").pack(), message_id=5)
    await H.tap(tg, BookingCb(a="pno", id=bid).pack(), message_id=9)
    b = await _get(world, bid)
    assert b.status == BookingStatus.CANCELLED_BY_CLIENT and not b.late_cancel


async def test_withdraw_pending(world: World, H: Harness) -> None:  # noqa: N803
    bid, c, tg = await _pending(world)
    await H.tap(tg, BookingCb(a="wd", id=bid).pack(), message_id=9)
    assert (await _get(world, bid)).status == BookingStatus.CANCELLED_BY_CLIENT


async def test_done_and_no_show_buttons_timing(world: World, H: Harness, clk: Clock) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    r = await book(world, c, L(D(0), "10:00"))
    bid = r.booking.id
    clk.now = L(D(0), "09:20")
    await H.tap(2000, BookingCb(a="card", id=bid).pack(), message_id=5)
    assert BookingCb(a="done", id=bid).pack() not in H.last_callbacks(2000)
    await H.tap(2000, BookingCb(a="ns", id=bid).pack(), message_id=5)  # crafted, too early
    assert (await _get(world, bid)).status == BookingStatus.CONFIRMED
    clk.now = L(D(0), "10:10")
    await H.tap(2000, BookingCb(a="card", id=bid).pack(), message_id=5)
    cbs = H.last_callbacks(2000)
    assert BookingCb(a="done", id=bid).pack() in cbs and BookingCb(a="ns", id=bid).pack() not in cbs
    clk.now = L(D(0), "10:30")
    await H.tap(2000, BookingCb(a="done", id=bid).pack(), message_id=5)
    assert (await _get(world, bid)).status == BookingStatus.COMPLETED
    told = "\n".join(H.texts_to(tg))
    assert "2/5" in told  # stamp + welcome
    assert any(x.startswith("rb:") for x in H.last_callbacks(tg))


async def test_staff_cancel_needs_reason(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    r = await book(world, c, L(D(1), "10:00"))
    await H.tap(2000, BookingCb(a="scx", id=r.booking.id).pack(), message_id=5)
    await H.text(2000, "   ")
    assert (await _get(world, r.booking.id)).status == BookingStatus.CONFIRMED
    await H.text(2000, "Kasal bo'lib qoldim")
    b = await _get(world, r.booking.id)
    assert b.status == BookingStatus.CANCELLED_BY_BARBER
    assert H.texts_to(tg)


async def test_busy_now_button(world: World, H: Harness, clk: Clock) -> None:  # noqa: N803
    from app.bot.callbacks import BusyCb
    from app.services import schedule

    clk.now = L(D(0), "14:07")
    await H.tap(2000, BusyCb(m=30).pack(), message_id=5)
    async with read_tx(world.sm) as s:
        offs = await schedule.list_time_off(
            s, barber_id=world.ali_id, start=L(D(0), "00:00"), end=L(D(1), "00:00")
        )
    assert [(o.start_at, o.end_at) for o in offs] == [(L(D(0), "14:00"), L(D(0), "14:45"))]


async def test_hub_buttons_render(world: World, H: Harness) -> None:  # noqa: N803
    c = await world.new_client()
    tg = await _tg(world, c)
    for a in ("bonus", "info", "prices", "my", "home"):
        n = len(H.session.calls)
        await H.tap(tg, HubCb(a=a).pack(), message_id=5)
        assert len(H.session.calls) > n, a
    assert not [x for x in H.alerts() if x == t("uz", "err.error")]
    assert timedelta
