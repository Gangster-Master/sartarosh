"""Mini App REST API through an in-process aiohttp client (docs/WEBAPP.md §4, §7).

Clock pinned at NOW = Mon 2026-10-26 09:00 Tashkent; Ali works Mon–Sat 10–20.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, catalog, slots, users
from app.services.dto import BookingCreated
from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.factories import MONDAY, NOW, D, L, World, book, new_client
from tests.webapp_harness import Api, Clock, make_api, tg_of


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest_asyncio.fixture
async def api(world: World, clk: Clock) -> AsyncIterator[Api]:
    a = await make_api(world.sm)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def _client(w: World, **kw) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    uid = await w.new_client(**kw)
    return uid, await tg_of(w.sm, uid)


async def _get(w: World, bid: int):  # type: ignore[no-untyped-def]
    async with read_tx(w.sm) as s:
        return await bookings.get_booking(s, bid)


def _create(w: World, start, **kw):  # type: ignore[no-untyped-def]
    body = {"barber_id": w.ali_id, "service_id": w.svc30, "start_at": iso(start)}
    body.update(kw)
    return body


async def _book(api: Api, tg: int, body: dict, key: str | None = None):  # type: ignore[no-untyped-def]
    return await api.post(
        tg, "/api/bookings", body, headers={"Idempotency-Key": key or str(uuid4())}
    )


# ----------------------------------------------------------------- wizard reads


async def test_wizard_reads_match_services(world: World, api: Api) -> None:
    _, tg = await _client(world)
    await world.new_barber("Bekzod")
    st, body = await api.get(tg, "/api/booking/services")
    async with read_tx(world.sm) as s:
        expect = [x.id for x in await catalog.list_bookable_services(s)]
    assert st == 200 and body["has_services"] is True
    assert [x["id"] for x in body["services"]] == expect

    st, body = await api.get(tg, f"/api/booking/barbers?service_id={world.svc30}")
    assert st == 200 and len(body["barbers"]) == 2
    assert body["any"]["available"] is True and body["any"]["price_uzs"] == 60_000

    st, body = await api.get(
        tg, f"/api/booking/days?barber_id={world.ali_id}&service_id={world.svc30}"
    )
    async with read_tx(world.sm) as s:
        days = await slots.get_available_days(s, barber_id=world.ali_id, service_id=world.svc30)
    assert [d["day"] for d in body["days"]] == [d.day.isoformat() for d in days]
    assert body["days"][0]["is_today"] is True and body["days"][0]["label"] == "26-oktabr, dushanba"
    assert body["days"][6]["has_free_slots"] is False  # Sunday off

    st, body = await api.get(
        tg, f"/api/booking/slots?barber_id={world.ali_id}&service_id={world.svc30}&day={MONDAY}"
    )
    async with read_tx(world.sm) as s:
        free = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=MONDAY
        )
    assert [x["start_at"] for x in body["slots"]] == [iso(x) for x in free]
    assert body["slots"][0]["time"] == "10:00" and body["slots"][0]["group"] == "morning"

    st, body = await api.get(tg, f"/api/booking/slots?service_id={world.svc30}&day={MONDAY}")
    assert st == 200 and body["slots"]  # "Farqi yo'q"

    st, body = await api.get(tg, f"/api/booking/slots?barber_id={world.ali_id}&day={D(30)}")
    assert st == 400 and body["error"]["field"] == "day"
    st, body = await api.get(tg, f"/api/booking/slots?barber_id={world.ali_id}&day={D(-1)}")
    assert st == 400


async def test_home_barbers_bonus(world: World, api: Api) -> None:
    uid, tg = await _client(world)
    await book(world, uid, L(MONDAY, "11:00"))
    st, home = await api.get(tg, "/api/home")
    assert st == 200
    assert home["upcoming"]["local"]["time"] == "11:00" and home["upcoming_count"] == 1
    assert home["bonus"]["mode"] == "stamps" and home["is_barber"] is False
    assert len(home["shop"]["hours"]) == 7 and home["shop"]["hours"][0]["open"] == "10:00"
    st, body = await api.get(tg, "/api/barbers")
    assert st == 200 and body["barbers"][0]["name"] == "Ali"
    assert {x["service_id"] for x in body["barbers"][0]["services"]} == {world.svc30, world.svc60}
    st, body = await api.get(tg, "/api/bonus")
    async with read_tx(world.sm) as s:
        code = (await users.get_user(s, uid)).ref_code
    assert body["referral"]["link"] == f"https://t.me/test_bot?start=ref_{code}"
    assert isinstance(body["bonus"]["recent"], list)


async def test_quote(world: World, api: Api) -> None:
    _, tg = await _client(world)
    st, q = await api.post(tg, "/api/booking/quote", _create(world, L(MONDAY, "11:00")))
    assert st == 200
    assert q["price_uzs"] == 60_000 and q["slot_free"] is True and q["phone_ok"] is True
    assert q["barber"]["name"] == "Ali" and q["local"]["end_time"] == "11:30"
    assert q["bonus"]["possible"] is False
    st, q = await api.post(tg, "/api/booking/quote", _create(world, L(MONDAY, "13:00")))
    assert q["slot_free"] is False  # lunch break


# ----------------------------------------------------------------- create


async def test_create_pending_notifies_once_after_commit(world: World, api: Api) -> None:
    await world.set("new_client_requires_approval", "true")
    uid, tg = await _client(world)
    seen = []
    real = api.notifier.__getattr__("booking_created")

    async def check(res):  # type: ignore[no-untyped-def]
        b = await _get(world, res.booking.id)  # committed before the notifier runs
        seen.append(b.status)
        await real(res)

    api.notifier.booking_created = check
    st, body = await _book(api, tg, _create(world, L(MONDAY, "11:00"), comment="salom"))
    assert st == 201, body
    assert body["auto_confirmed"] is False and body["booking"]["status"] == "PENDING"
    assert body["booking"]["actions"]["withdraw"] is True
    assert body["booking"]["source"] == "bot" and body["booking"]["comment"] == "salom"
    assert seen == [BookingStatus.PENDING]
    assert api.notifier.count("booking_created") == 1
    assert isinstance(api.notifier.calls[0][1][0], BookingCreated)


async def test_slot_taken_and_limits(world: World, api: Api) -> None:
    _, a = await _client(world)
    _, b = await _client(world)
    st, _ = await _book(api, a, _create(world, L(MONDAY, "11:00")))
    assert st == 201
    st, body = await _book(api, b, _create(world, L(MONDAY, "11:00")))
    assert st == 409 and body["error"]["code"] == "slot_taken"

    await world.set("max_active_bookings", "1")
    st, body = await _book(api, a, _create(world, L(MONDAY, "15:00")))
    assert st == 409 and body["error"]["code"] == "client_limit"
    assert body["error"]["ctx"] == {"kind": "max_active", "limit": 1}

    nophone = await new_client(world.sm, verified=False)
    st, body = await _book(api, await tg_of(world.sm, nophone), _create(world, L(MONDAY, "16:00")))
    assert st == 422 and body["error"]["code"] == "phone_required"


async def test_start_at_parsing(world: World, api: Api) -> None:
    _, tg = await _client(world)
    st, _ = await _book(
        api,
        tg,
        _create(world, None, start_at=L(MONDAY, "12:00").isoformat()),  # +00:00 form
    )
    assert st == 201
    st, body = await _book(api, tg, _create(world, None, start_at="2026-10-26T10:00:30Z"))
    assert st == 400 and body["error"]["field"] == "start_at"
    st, body = await _book(api, tg, _create(world, None, start_at="2026-10-26T10:00:00"))
    assert st == 400 and body["error"]["field"] == "start_at"
    st, body = await _book(api, tg, _create(world, None, start_at="tomorrow"))
    assert st == 400
    st, body = await api.post(tg, "/api/bookings", _create(world, L(MONDAY, "15:00")))
    assert st == 400 and body["error"]["field"] == "idempotency_key"


async def test_comment_is_returned_as_text(world: World, api: Api) -> None:
    _, tg = await _client(world)
    evil = "<img src=x onerror=alert(1)>"
    st, body = await _book(api, tg, _create(world, L(MONDAY, "11:00"), comment=evil))
    assert st == 201 and body["booking"]["comment"] == evil
    st, body = await _book(api, tg, _create(world, L(MONDAY, "15:00"), comment="x" * 301))
    assert st == 400 and body["error"]["field"] == "comment"


async def test_idempotency_replay(world: World, api: Api) -> None:
    _, tg = await _client(world)
    body = _create(world, L(MONDAY, "11:00"))
    st1, r1 = await _book(api, tg, body, key="k-1")
    st2, r2 = await _book(api, tg, body, key="k-1")
    assert st1 == st2 == 201 and r1 == r2
    assert api.notifier.count("booking_created") == 1


async def test_concurrent_creates_are_serialised(
    file_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    Clock(monkeypatch, NOW)
    api = await make_api(file_world.sm)
    try:
        _, tg = await _client(file_world)
        body = _create(file_world, L(MONDAY, "11:00"))
        res = await asyncio.gather(_book(api, tg, body), _book(api, tg, body))
        assert sorted(st for st, _ in res) == [201, 409]
        assert api.notifier.count("booking_created") == 1
    finally:
        await api.client.close()


# ----------------------------------------------------------------- cancel / proposal


async def test_cancel_late_once(world: World, api: Api) -> None:
    uid, tg = await _client(world)
    r = await book(world, uid, L(MONDAY, "10:00"))  # CONFIRMED, starts in 1 h (< 2 h)
    bid = r.booking.id
    st, body = await api.get(tg, f"/api/bookings/{bid}")
    assert body["booking"]["actions"]["cancel"] is True
    assert body["booking"]["actions"]["late_cancel_warning"] is True
    st, body = await api.post(tg, f"/api/bookings/{bid}/cancel", {"withdraw": False})
    assert st == 200 and body["late"] is True
    assert body["booking"]["status"] == "CANCELLED_BY_CLIENT"
    st, body = await api.post(tg, f"/api/bookings/{bid}/cancel", {"withdraw": False})
    assert st == 200 and body["booking"]["status"] == "CANCELLED_BY_CLIENT"
    await drain_pending_notifications()
    assert api.notifier.count("booking_cancelled") == 1
    name, args, kwargs = api.notifier.calls[-1]
    assert args[0].id == bid and kwargs == {"by_client": True}
    async with read_tx(world.sm) as s:
        assert (await users.get_user(s, uid)).late_cancel_count == 1


async def test_concurrent_cancel_notifies_once(
    file_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    Clock(monkeypatch, NOW)
    api = await make_api(file_world.sm)
    try:
        uid, tg = await _client(file_world)
        bid = (await book(file_world, uid, L(MONDAY, "15:00"))).booking.id
        res = await asyncio.gather(
            *(api.post(tg, f"/api/bookings/{bid}/cancel", {"withdraw": False}) for _ in range(3))
        )
        assert [st for st, _ in res] == [200, 200, 200]
        await drain_pending_notifications()
        assert api.notifier.count("booking_cancelled") == 1
    finally:
        await api.client.close()


async def test_cancel_foreign_booking_forbidden(world: World, api: Api) -> None:
    a, _ = await _client(world)
    _, b_tg = await _client(world)
    bid = (await book(world, a, L(MONDAY, "15:00"))).booking.id
    st, body = await api.post(b_tg, f"/api/bookings/{bid}/cancel", {"withdraw": False})
    assert st == 403
    st, _ = await api.get(b_tg, f"/api/bookings/{bid}")
    assert st == 403
    assert (await _get(world, bid)).status == BookingStatus.CONFIRMED


async def test_withdraw_requires_pending(world: World, api: Api) -> None:
    uid, tg = await _client(world)
    bid = (await book(world, uid, L(MONDAY, "15:00"))).booking.id  # CONFIRMED
    st, body = await api.post(tg, f"/api/bookings/{bid}/cancel", {"withdraw": True})
    assert st == 409 and body["error"]["code"] == "invalid_transition"


async def test_proposal_answer(world: World, api: Api) -> None:
    await world.set("new_client_requires_approval", "true")
    uid, tg = await _client(world)
    bid = (await book(world, uid, L(MONDAY, "15:00"))).booking.id
    async with write_tx(world.sm) as s:
        await bookings.propose_time(
            s, booking_id=bid, actor_user_id=world.ali_user_id, new_start_at=L(MONDAY, "16:00")
        )
    st, body = await api.get(tg, f"/api/bookings/{bid}")
    assert body["booking"]["actions"]["answer_proposal"] is True
    assert body["booking"]["original_local"]["time"] == "15:00"
    for _ in range(2):
        st, body = await api.post(tg, f"/api/bookings/{bid}/proposal", {"accept": True})
        assert st == 200 and body["booking"]["status"] == "CONFIRMED"
    await drain_pending_notifications()
    assert api.notifier.count("proposal_answered") == 1


# ----------------------------------------------------------------- barber panel


async def test_barber_accept_reject_once(world: World, api: Api) -> None:
    await world.set("new_client_requires_approval", "true")
    ali = await tg_of(world.sm, world.ali_user_id)
    c1, _ = await _client(world)
    c2, _ = await _client(world)
    b1 = (await book(world, c1, L(MONDAY, "15:00"))).booking.id
    b2 = (await book(world, c2, L(MONDAY, "16:00"))).booking.id

    st, q = await api.get(ali, "/api/barber/queue")
    assert st == 200 and q["pending_count"] == 2 and q["today"] == "2026-10-26"
    card = next(x for x in q["bookings"] if x["id"] == b1)
    assert card["actions"]["accept"] and card["actions"]["reject"]
    assert card["client"]["stats"]["is_new"] is True

    for _ in range(2):
        st, body = await api.post(ali, f"/api/barber/bookings/{b1}/accept")
        assert st == 200 and body["booking"]["status"] == "CONFIRMED"
    for _ in range(2):
        st, body = await api.post(ali, f"/api/barber/bookings/{b2}/reject", {"reason": "busy"})
        assert st == 200 and body["booking"]["status"] == "REJECTED"
        assert body["booking"]["cancel_reason_label"] == "Band"
    st, body = await api.post(ali, f"/api/barber/bookings/{b1}/reject", {"reason": None})
    assert st == 409 and body["error"]["code"] == "invalid_transition"
    await drain_pending_notifications()
    assert api.notifier.count("booking_confirmed") == 1
    assert api.notifier.count("booking_rejected") == 1


async def test_barber_done_and_no_show_once(world: World, api: Api, clk: Clock) -> None:
    ali = await tg_of(world.sm, world.ali_user_id)
    c1, _ = await _client(world)
    c2, _ = await _client(world)
    b1 = (await book(world, c1, L(MONDAY, "10:00"))).booking.id
    b2 = (await book(world, c2, L(MONDAY, "10:30"))).booking.id
    st, body = await api.post(ali, f"/api/barber/bookings/{b1}/done")
    assert st == 409 and body["error"]["code"] == "too_early"
    st, q = await api.get(ali, "/api/barber/queue?day=2026-10-26")
    card = next(x for x in q["bookings"] if x["id"] == b1)
    assert card["actions"]["done_from"] == iso(L(MONDAY, "09:30"))
    assert card["actions"]["no_show_from"] == iso(L(MONDAY, "10:15"))

    clk.now = L(MONDAY, "10:50")
    st, body = await api.post(ali, f"/api/barber/bookings/{b1}/done")
    assert st == 200 and body["booking"]["status"] == "COMPLETED" and body["notes"]
    assert "<" not in "".join(body["notes"])
    st, body = await api.post(ali, f"/api/barber/bookings/{b1}/done")
    assert st == 200 and body["notes"] == []
    for _ in range(2):
        st, body = await api.post(ali, f"/api/barber/bookings/{b2}/no-show")
        assert st == 200 and body["booking"]["status"] == "NO_SHOW"
    await drain_pending_notifications()
    assert api.notifier.count("booking_completed") == 1
    assert api.notifier.count("booking_no_show") == 1


async def test_other_barber_cannot_touch(world: World, api: Api) -> None:
    await world.set("new_client_requires_approval", "true")
    _, bek_user = await world.new_barber("Bekzod")
    bek = await tg_of(world.sm, bek_user)
    c, _ = await _client(world)
    bid = (await book(world, c, L(MONDAY, "15:00"))).booking.id
    for action in ("accept", "reject", "done", "no-show"):
        st, body = await api.post(bek, f"/api/barber/bookings/{bid}/{action}")
        assert st == 403, (action, body)
    assert (await _get(world, bid)).status == BookingStatus.PENDING
    st, _ = await api.get(bek, f"/api/bookings/{bid}")
    assert st == 403
    assert api.notifier.calls == []


async def test_barber_queue_day_range(world: World, api: Api) -> None:
    ali = await tg_of(world.sm, world.ali_user_id)
    st, body = await api.get(ali, f"/api/barber/queue?day={D(21)}")
    assert st == 400 and body["error"]["field"] == "day"
    st, body = await api.get(ali, f"/api/barber/queue?day={D(20)}")
    assert st == 200
    st, days = await api.get(ali, "/api/barber/days")
    assert len(days["days"]) == 7 and days["days"][0]["is_today"] is True


# ----------------------------------------------------------------- errors


async def test_unexpected_error_hides_details(
    world: World, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tg = await _client(world)

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("secret detail")

    monkeypatch.setattr(bookings, "list_client_bookings", boom)
    st, body = await api.get(tg, "/api/bookings")
    assert st == 500 and body["error"]["code"] == "error"
    assert "secret" not in str(body)


async def test_photo_endpoint(world: World, api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    st, body = await api.get(None, f"/api/barbers/{world.ali_id}/photo")
    assert st == 404  # no photo
    st, _ = await api.get(None, "/api/barbers/999/photo")
    assert st == 404
    async with write_tx(world.sm) as s:
        from app.services import barbers

        await barbers.update_barber(
            s, actor_user_id=world.admin_id, barber_id=world.ali_id, photo_file_id="FILE1"
        )
    photos = api.client.app[__import__("app.webapp.auth", fromlist=["x"]).PHOTOS_KEY]
    calls = []

    async def fake_download(file_id):  # type: ignore[no-untyped-def]
        calls.append(file_id)
        return b"\xff\xd8jpeg"

    photos.bot = object()  # type: ignore[assignment]
    monkeypatch.setattr(photos, "_download", fake_download)
    for _ in range(2):
        async with api.client.get(f"/api/barbers/{world.ali_id}/photo?v=1") as r:
            assert r.status == 200 and r.content_type == "image/jpeg"
            assert await r.read() == b"\xff\xd8jpeg"
    assert calls == ["FILE1"]


async def test_tomorrow_label(world: World, api: Api, clk: Clock) -> None:
    clk.now = NOW + timedelta(hours=14)  # 23:00 local, still Monday
    _, tg = await _client(world)
    st, body = await api.get(tg, "/api/booking/days")
    assert body["today"] == "2026-10-26" and body["days"][1]["is_tomorrow"] is True


async def test_barber_ratings_public_in_mini_app(world: World, api: Api) -> None:
    """Same public rating as the bot (rating_badge): avg + count, avg null before reviews."""
    from tests.test_public_rating import _rated_visit

    for day, rating in enumerate([5, 4, 4]):
        await _rated_visit(world, day, rating)
    other_id, _ = await world.new_barber("Bekzod")
    _, tg = await _client(world)
    st, body = await api.get(tg, "/api/barbers")
    got = {b["id"]: b for b in body["barbers"]}
    assert st == 200
    assert (got[world.ali_id]["rating_avg"], got[world.ali_id]["rating_count"]) == (4.3, 3)
    assert (got[other_id]["rating_avg"], got[other_id]["rating_count"]) == (None, 0)
    st, body = await api.get(tg, f"/api/booking/barbers?service_id={world.svc30}")
    got = {b["id"]: b for b in body["barbers"]}
    assert st == 200
    assert (got[world.ali_id]["rating_avg"], got[world.ali_id]["rating_count"]) == (4.3, 3)
    assert (got[other_id]["rating_avg"], got[other_id]["rating_count"]) == (None, 0)
