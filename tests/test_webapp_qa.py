"""QA gaps for the Mini App: spec-level initData check, IDOR, roles, bonus redeem,
slots after a web booking, "Farqi yo'q", static files.

Clock pinned at NOW = Mon 2026-10-26 09:00 Tashkent; Ali works Mon–Sat 10–20.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncIterator
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from yarl import URL

from app.db.enums import BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bonus, bookings, slots, users
from app.webapp import server
from app.webapp.auth import InitDataError, validate_init_data
from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.factories import MONDAY, NOW, L, World, book
from tests.webapp_harness import TOKEN, Api, Clock, make_api, tg_of


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest_asyncio.fixture
async def api(world: World, clk: Clock) -> AsyncIterator[Api]:
    a = await make_api(world.sm)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def _client(w: World) -> tuple[int, int]:
    uid = await w.new_client()
    return uid, await tg_of(w.sm, uid)


def _body(w: World, start, **kw):  # type: ignore[no-untyped-def]
    b = {"barber_id": w.ali_id, "service_id": w.svc30, "start_at": iso(start)}
    b.update(kw)
    return b


async def _book(api: Api, tg: int, body: dict):  # type: ignore[no-untyped-def]
    return await api.post(tg, "/api/bookings", body, headers={"Idempotency-Key": str(uuid4())})


# ----------------------------------------------------------------- initData (spec)


def _telegram_reference(fields: dict[str, str], token: str) -> str:
    """Independent implementation of core.telegram.org/bots/webapps#validating-data."""
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(key=b"WebAppData", msg=token.encode(), digestmod=hashlib.sha256).digest()
    h = hmac.new(key=secret, msg=dcs.encode(), digestmod=hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def test_matches_independent_reference_implementation() -> None:
    user = {
        "id": 7,
        "first_name": "Ali",
        "last_name": "Valiyev",
        "username": "ali",
        "language_code": "uz",
    }
    raw = _telegram_reference(
        {
            "query_id": "AAH",
            "user": json.dumps(user, ensure_ascii=False),
            "auth_date": str(int(time.time())),
            "signature": "abc",
            "chat_instance": "-1",
        },
        TOKEN,
    )
    d = validate_init_data(raw, TOKEN, max_age=60)
    assert (d.user_id, d.last_name, d.username) == (7, "Valiyev", "ali")
    # uppercase hash is not what Telegram sends but must not crash
    with pytest.raises(InitDataError) as ei:
        validate_init_data(raw.replace("auth_date=", "auth_date=1"), TOKEN, max_age=60)
    assert ei.value.reason == "bad_hash"


async def test_wrong_scheme_and_expired_http(world: World, api: Api) -> None:
    _, tg = await _client(world)
    from tests.webapp_harness import init_data

    st, body = await api.get(None, "/api/me", auth="Bearer " + init_data(tg))
    assert st == 401 and body["error"]["reason"] == "missing"
    old = int(time.time()) - 2 * 86400
    st, body = await api.get(tg, "/api/me", auth_date=old)
    assert st == 401 and body["error"]["reason"] == "expired"
    st, body = await api.get(tg, "/api/me", user=None)
    assert st == 401 and body["error"]["reason"] == "no_user"


# ----------------------------------------------------------------- IDOR / roles


async def test_lists_only_own_bookings(world: World, api: Api) -> None:
    a, a_tg = await _client(world)
    b, b_tg = await _client(world)
    ba = (await book(world, a, L(MONDAY, "11:00"))).booking.id
    bb = (await book(world, b, L(MONDAY, "15:00"))).booking.id
    st, body = await api.get(a_tg, "/api/bookings")
    ids = {x["id"] for x in body["upcoming"] + body["past"]}
    assert st == 200 and ba in ids and bb not in ids
    st, home = await api.get(b_tg, "/api/home")
    assert home["upcoming"]["id"] == bb
    # a client never gets the barber view (no client phone) of its own booking
    st, body = await api.get(a_tg, f"/api/bookings/{ba}")
    assert body["viewer"] == "client" and "client" not in body["booking"]
    st, _ = await api.get(a_tg, "/api/bookings/999999")
    assert st == 404


async def test_proposal_on_foreign_booking_forbidden(world: World, api: Api) -> None:
    a, _ = await _client(world)
    _, b_tg = await _client(world)
    bid = (await book(world, a, L(MONDAY, "15:00"))).booking.id
    st, _ = await api.post(b_tg, f"/api/bookings/{bid}/proposal", {"accept": True})
    assert st == 403


async def test_other_barber_cannot_view_booking(world: World, api: Api) -> None:
    c, _ = await _client(world)
    _, bek_user = await world.new_barber("Bekzod")
    bek_tg = await tg_of(world.sm, bek_user)
    bid = (await book(world, c, L(MONDAY, "15:00"))).booking.id
    st, _ = await api.get(bek_tg, f"/api/bookings/{bid}")
    assert st == 403
    ali_tg = await tg_of(world.sm, world.ali_user_id)
    st, body = await api.get(ali_tg, f"/api/bookings/{bid}")
    assert st == 200 and body["viewer"] == "barber" and body["booking"]["client"]["phone"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/barber/days"),
        ("GET", "/api/barber/queue"),
        ("POST", "/api/barber/bookings/{id}/accept"),
        ("POST", "/api/barber/bookings/{id}/reject"),
        ("POST", "/api/barber/bookings/{id}/done"),
        ("POST", "/api/barber/bookings/{id}/no-show"),
    ],
)
async def test_client_and_plain_admin_cannot_use_barber_api(
    world: World, api: Api, method: str, path: str
) -> None:
    await world.set("new_client_requires_approval", "true")
    c, c_tg = await _client(world)
    bid = (await book(world, c, L(MONDAY, "15:00"))).booking.id  # PENDING for Ali
    admin_tg = await tg_of(world.sm, world.admin_id)
    for tg in (c_tg, admin_tg):
        st, body = await api.req(method, path.format(id=bid), tg, {} if method == "POST" else None)
        assert st == 403 and body["error"]["code"] == "forbidden"
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, bid)).status == BookingStatus.PENDING
    assert api.notifier.calls == []


async def test_blocked_user_cannot_book_or_cancel(world: World, api: Api) -> None:
    c, tg = await _client(world)
    bid = (await book(world, c, L(MONDAY, "15:00"))).booking.id
    async with write_tx(world.sm) as s:
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=c, blocked=True)
    st, body = await _book(api, tg, _body(world, L(MONDAY, "16:00")))
    assert st == 403 and body["error"]["code"] == "user_blocked"
    st, body = await api.post(tg, f"/api/bookings/{bid}/cancel", {"withdraw": False})
    assert st == 403


# ----------------------------------------------------------------- rules shared with the bot


async def test_cancel_policy_matches_bot(world: World, api: Api, clk: Clock) -> None:
    c, tg = await _client(world)
    far = (await book(world, c, L(MONDAY, "17:00"))).booking.id  # > free_cancel_hours away
    st, body = await api.post(tg, f"/api/bookings/{far}/cancel", {"withdraw": False})
    assert st == 200 and body["late"] is False
    async with read_tx(world.sm) as s:
        assert (await users.get_user(s, c)).late_cancel_count == 0

    past = (await book(world, c, L(MONDAY, "11:00"))).booking.id
    clk.now = L(MONDAY, "11:05")  # already started: the bot refuses too
    st, body = await api.post(tg, f"/api/bookings/{past}/cancel", {"withdraw": False})
    assert st == 409 and body["error"]["code"] == "invalid_transition"
    async with read_tx(world.sm) as s:
        assert (await bookings.get_booking(s, past)).status == BookingStatus.CONFIRMED


async def test_bonus_redeem_via_api(world: World, api: Api) -> None:
    c, tg = await _client(world)
    # no stamps yet: asking for the bonus is refused like in the bot
    st, body = await _book(api, tg, _body(world, L(MONDAY, "11:00"), use_bonus=True))
    assert st == 409 and body["error"]["code"] == "no_bonus"
    async with write_tx(world.sm) as s:
        await bonus.adjust_bonus(s, actor_user_id=world.admin_id, user_id=c, delta=5, comment="qa")
    st, q = await api.post(tg, "/api/booking/quote", _body(world, L(MONDAY, "11:00")))
    assert q["bonus"]["possible"] is True and q["bonus"]["final_price_uzs"] == 0
    st, body = await _book(api, tg, _body(world, L(MONDAY, "11:00"), use_bonus=True))
    assert st == 201 and body["booking"]["bonus_cost"] == 5
    assert body["booking"]["final_price_uzs"] == 0
    async with read_tx(world.sm) as s:
        stt = await bonus.get_bonus_status(s, c)
    assert stt.reserved == 5 and stt.available == 0
    # the stamps are reserved: a second redeem is refused
    st, body = await _book(api, tg, _body(world, L(MONDAY, "15:00"), use_bonus=True))
    assert st == 409 and body["error"]["code"] == "no_bonus"


async def test_slots_after_web_booking_and_double_booking(world: World, api: Api) -> None:
    _, a = await _client(world)
    _, b = await _client(world)
    url = f"/api/booking/slots?barber_id={world.ali_id}&service_id={world.svc30}&day={MONDAY}"
    st, before = await api.get(a, url)
    taken = L(MONDAY, "11:00")
    assert iso(taken) in [x["start_at"] for x in before["slots"]]
    assert all(x["start_at"] != iso(L(MONDAY, "13:00")) for x in before["slots"])  # break
    st, _ = await _book(api, a, _body(world, taken))
    assert st == 201
    st, after = await api.get(b, url)
    starts = [x["start_at"] for x in after["slots"]]
    assert iso(taken) not in starts
    async with read_tx(world.sm) as s:
        free = await slots.get_free_slots(
            s, barber_id=world.ali_id, service_id=world.svc30, day=MONDAY
        )
    assert starts == [iso(x) for x in free]
    # overlapping 60-min booking at 10:30 is rejected too
    st, body = await _book(api, b, _body(world, L(MONDAY, "10:30"), service_id=world.svc60))
    assert st == 409 and body["error"]["code"] in ("slot_taken", "slot_unavailable")
    st, q = await api.post(b, "/api/booking/quote", _body(world, taken))
    assert q["slot_free"] is False


async def test_any_barber_booking(world: World, api: Api) -> None:
    bek_id, _ = await world.new_barber("Bekzod")
    _, tg = await _client(world)
    st, body = await api.get(tg, f"/api/booking/slots?service_id={world.svc30}&day={MONDAY}")
    async with read_tx(world.sm) as s:
        free = await slots.get_free_slots_any(s, service_id=world.svc30, day=MONDAY)
    assert [x["start_at"] for x in body["slots"]] == [iso(x) for x in free]
    st, body = await _book(api, tg, _body(world, L(MONDAY, "11:00"), barber_id=None))
    assert st == 201 and body["booking"]["barber"]["id"] in (world.ali_id, bek_id)
    assert api.notifier.count("booking_created") == 1
    res = api.notifier.calls[0][1][0]
    assert res.booking.id == body["booking"]["id"]


async def test_pending_booking_then_barber_accepts_via_api(world: World, api: Api) -> None:
    await world.set("new_client_requires_approval", "true")
    _, tg = await _client(world)
    st, body = await _book(api, tg, _body(world, L(MONDAY, "15:00")))
    assert st == 201 and body["auto_confirmed"] is False
    bid = body["booking"]["id"]
    assert body["booking"]["actions"]["withdraw"] is True
    ali_tg = await tg_of(world.sm, world.ali_user_id)
    st, q = await api.get(ali_tg, f"/api/barber/queue?day={MONDAY}")
    item = next(x for x in q["bookings"] if x["id"] == bid)
    assert item["actions"]["accept"] is True and q["pending_count"] == 1
    st, body = await api.post(ali_tg, f"/api/barber/bookings/{bid}/accept")
    assert st == 200 and body["booking"]["status"] == "CONFIRMED"
    await drain_pending_notifications()
    assert api.notifier.names() == ["booking_created", "booking_confirmed"]


# ----------------------------------------------------------------- static files


async def test_static_files_served(world: World, api: Api) -> None:
    async with api.client.get("/") as r:
        html = await r.text()
        assert r.status == 200 and "telegram-web-app.js" in html
    refs = set(re.findall(r'(?:src|href)="(static/[^"]+)"', html))
    assert "static/app.js" in refs
    js = sorted(
        p.relative_to(server.STATIC_DIR).as_posix() for p in server.STATIC_DIR.rglob("*.js")
    )
    for rel in sorted(refs) + [f"static/{p}" for p in js]:
        async with api.client.get("/" + rel) as r:
            assert r.status == 200, rel
            if rel.endswith(".js"):
                assert "javascript" in r.headers["Content-Type"], rel
            assert r.headers["X-Content-Type-Options"] == "nosniff"
    for raw in ("/static/..%2f..%2fconfig.py", "/static/%2e%2e/%2e%2e/config.py", "/static/"):
        async with api.client.get(URL(raw, encoded=True)) as r:
            assert r.status in (403, 404), raw
    async with api.client.get("/api/nope") as r:
        assert r.status == 404 and (await r.json())["error"]["code"] == "not_found"


async def test_bonus_redeemed_when_barber_marks_done(world: World, api: Api, clk: Clock) -> None:
    await world.set("welcome_bonus", "0")
    c, tg = await _client(world)
    async with write_tx(world.sm) as s:
        await bonus.adjust_bonus(s, actor_user_id=world.admin_id, user_id=c, delta=5, comment="qa")
    st, body = await _book(api, tg, _body(world, L(MONDAY, "11:00"), use_bonus=True))
    bid = body["booking"]["id"]
    clk.now = L(MONDAY, "11:30")
    ali_tg = await tg_of(world.sm, world.ali_user_id)
    st, body = await api.post(ali_tg, f"/api/barber/bookings/{bid}/done")
    assert st == 200 and body["booking"]["status"] == "COMPLETED"
    assert body["booking"]["bonus_redeemed"] is True and body["booking"]["final_price_uzs"] == 0
    async with read_tx(world.sm) as s:
        stt = await bonus.get_bonus_status(s, c)
    assert stt.reserved == 0 and stt.balance == 0
    await drain_pending_notifications()
    assert api.notifier.count("booking_completed") == 1
