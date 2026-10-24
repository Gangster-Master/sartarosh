"""Mini App auth: initData HMAC, freshness, user resolution and gates (docs/WEBAPP.md §3)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.db.uow import read_tx, write_tx
from app.services import users
from app.webapp import auth
from app.webapp.auth import InitDataError, validate_init_data
from tests.factories import NOW, World, new_client
from tests.webapp_harness import TOKEN, Api, Clock, contact_response, init_data, make_api, tg_of


@pytest_asyncio.fixture
async def api(world: World, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    Clock(monkeypatch, NOW)
    a = await make_api(world.sm)
    yield a
    await a.client.close()


# ----------------------------------------------------------------- pure validation


def test_valid_signature_parses_user() -> None:
    d = validate_init_data(
        init_data(42, first_name="Ali", username="ali", start_param="ref_abc"), TOKEN, max_age=60
    )
    assert (d.user_id, d.first_name, d.username, d.start_param) == (42, "Ali", "ali", "ref_abc")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (lambda: init_data(42, token="999:other"), "bad_hash"),
        (lambda: init_data(42).replace("hash=", "nohash="), "bad_init_data"),
        (lambda: init_data(42, auth_date=int(time.time()) - 3600), "expired"),
        (lambda: init_data(42, auth_date=int(time.time()) + 3600), "bad_init_data"),
        (lambda: init_data(42, user=None), "no_user"),
        (lambda: init_data(42, user={"first_name": "x"}), "no_user"),
        (lambda: init_data(42) + "&auth_date=1", "bad_init_data"),  # duplicate key
        (lambda: "x" * 5000, "bad_init_data"),
        (lambda: "", "missing"),
    ],
)
def test_invalid_init_data(raw, reason) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InitDataError) as ei:
        validate_init_data(raw(), TOKEN, max_age=600)
    assert ei.value.reason == reason


def test_tampered_field_fails() -> None:
    good = init_data(42, first_name="Ali")
    with pytest.raises(InitDataError) as ei:
        validate_init_data(good.replace("Ali", "Bob"), TOKEN, max_age=600)
    assert ei.value.reason == "bad_hash"


def test_signature_field_stays_in_check_string() -> None:
    raw = init_data(42, extra={"signature": "abc"})
    assert validate_init_data(raw, TOKEN, max_age=600).user_id == 42


# ----------------------------------------------------------------- HTTP gate


async def test_http_401_shapes(api: Api) -> None:
    st, body = await api.get(None, "/api/me")
    assert st == 401 and body["error"]["code"] == "unauthorized"
    assert body["error"]["reason"] == "missing"
    st, body = await api.get(None, "/api/me", auth="tma " + init_data(7, token="1:x"))
    assert st == 401 and body["error"]["reason"] == "bad_hash"
    st, body = await api.get(None, "/api/me", auth="Bearer " + init_data(7))
    assert st == 401


async def test_write_requests_use_shorter_max_age(world: World) -> None:
    # default write age is 24 h since the judge pass; a shorter one is still honoured
    api = await make_api(world.sm, auth_max_age_write=3600)
    try:
        c = await world.new_client()
        tg = await tg_of(world.sm, c)
        old = int(time.time()) - 7200  # fine for GET (24 h), too old for POST (1 h)
        st, _ = await api.get(tg, "/api/me", auth_date=old)
        assert st == 200
        st, body = await api.post(tg, "/api/me/consent", auth_date=old)
        assert st == 401 and body["error"]["reason"] == "expired"
    finally:
        await api.client.close()


async def test_default_write_age_survives_a_working_day(world: World, api: Api) -> None:
    c = await world.new_client()
    tg = await tg_of(world.sm, c)
    old = int(time.time()) - 10 * 3600  # barber opened the panel in the morning
    st, _ = await api.post(tg, "/api/me/consent", auth_date=old)
    assert st == 200


async def test_new_user_created_with_referral(world: World, api: Api) -> None:
    ref = await world.new_client()
    async with read_tx(world.sm) as s:
        code = (await users.get_user(s, ref)).ref_code
    st, body = await api.get(777001, "/api/me", first_name="Vali", start_param=f"ref_{code}")
    assert st == 200
    assert body["user"]["consent_required"] is True
    assert "consent_text" in body and "<" not in body["consent_text"]
    assert body["bot_username"] == "test_bot"
    async with read_tx(world.sm) as s:
        ctx = await users.load_context(s, tg_id=777001)
    assert ctx is not None and ctx.user.first_name == "Vali"
    assert ctx.user.referrer_id == ref


async def test_second_request_does_not_write_user(
    world: World, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    await api.get(777002, "/api/me", first_name="Vali", start_param="src_insta")
    calls = []
    real = users.ensure_user

    async def spy(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(k)
        return await real(*a, **k)

    monkeypatch.setattr(users, "ensure_user", spy)
    st, _ = await api.get(777002, "/api/me", first_name="Vali", start_param="src_insta")
    assert st == 200 and calls == []
    st, _ = await api.get(777002, "/api/me", first_name="Valijon")  # profile change -> sync
    assert st == 200 and len(calls) == 1


async def test_consent_gate(world: World, api: Api) -> None:
    c = await world.new_client(consent=False)
    tg = await tg_of(world.sm, c)
    st, body = await api.get(tg, "/api/bookings")
    assert st == 403 and body["error"]["code"] == "consent_required"
    st, body = await api.get(tg, "/api/me")
    assert st == 200 and body["user"]["consent_required"] is True
    st, body = await api.post(tg, "/api/me/consent")
    assert st == 200 and body["user"]["consent_required"] is False
    st, _ = await api.get(tg, "/api/bookings")
    assert st == 200


async def test_blocked_user(world: World, api: Api) -> None:
    c = await world.new_client()
    tg = await tg_of(world.sm, c)
    async with write_tx(world.sm) as s:
        await users.set_blocked(s, actor_user_id=world.admin_id, user_id=c, blocked=True)
    for path in ("/api/me", "/api/home"):
        st, body = await api.get(tg, path)
        assert st == 403 and body["error"]["code"] == "user_blocked"


async def test_non_barber_forbidden_on_barber_api(world: World, api: Api) -> None:
    c = await world.new_client()
    tg = await tg_of(world.sm, c)
    st, body = await api.get(tg, "/api/barber/days")
    assert st == 403 and body["error"]["code"] == "forbidden"
    st, body = await api.post(tg, "/api/barber/bookings/1/accept")
    assert st == 403
    ali_tg = await tg_of(world.sm, world.ali_user_id)
    st, body = await api.get(ali_tg, "/api/barber/days")
    assert st == 200 and body["barber"]["id"] == world.ali_id


async def test_post_requires_json_content_type(world: World, api: Api) -> None:
    c = await world.new_client()
    tg = await tg_of(world.sm, c)
    async with api.client.post(
        "/api/me/consent",
        data="{}",
        headers={"Authorization": "tma " + init_data(tg), "Content-Type": "text/plain"},
    ) as r:
        body = await r.json()
    assert r.status == 400 and body["error"]["field"] == "body"


async def test_contact_response(world: World, api: Api) -> None:
    uid = await new_client(world.sm, verified=False)
    tg = await tg_of(world.sm, uid)
    st, body = await api.get(tg, "/api/me")
    assert body["user"]["phone_verified"] is False
    # foreign user_id in a correctly signed response
    st, body = await api.post(
        tg, "/api/me/contact", {"response": contact_response(tg, "+998901112233", owner=tg + 1)}
    )
    assert st == 403 and body["error"]["code"] == "forbidden"
    # bad signature
    st, body = await api.post(
        tg, "/api/me/contact", {"response": contact_response(tg, "+998901112233", token="1:x")}
    )
    assert st == 400 and body["error"]["field"] == "contact"
    st, body = await api.post(
        tg, "/api/me/contact", {"response": contact_response(tg, "998901112233")}
    )
    assert st == 200
    assert body["user"]["phone"] == "+998901112233" and body["user"]["phone_verified"] is True


async def test_rate_limit_before_db(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, NOW)
    api = await make_api(world.sm, read_per_minute=2)
    try:
        c = await world.new_client()
        tg = await tg_of(world.sm, c)
        calls = []
        real = auth.resolve_user

        async def spy(sm, init):  # type: ignore[no-untyped-def]
            calls.append(init.user_id)
            return await real(sm, init)

        monkeypatch.setattr(auth, "resolve_user", spy)
        assert (await api.get(tg, "/api/me"))[0] == 200
        assert (await api.get(tg, "/api/me"))[0] == 200
        st, body = await api.get(tg, "/api/me")
        assert st == 429 and body["error"]["code"] == "throttled"
        assert len(calls) == 2
    finally:
        await api.client.close()


async def test_unauthorized_limited_per_ip(api: Api) -> None:
    statuses = [(await api.get(None, "/api/me"))[0] for _ in range(61)]
    assert statuses[:60] == [401] * 60 and statuses[60] == 429


async def test_headers_and_health(api: Api) -> None:
    st, body = await api.get(None, "/healthz")
    assert st == 200 and body == {"ok": True}
    h = api.last_headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors" in h["Content-Security-Policy"]
    assert "X-Frame-Options" not in h
    st, _ = await api.req("OPTIONS", "/api/me", None, headers={"Origin": "https://evil.example"})
    assert not any(k.lower().startswith("access-control-") for k in api.last_headers)
    st, body = await api.get(None, "/api/nope")
    assert st == 404 and body["error"]["code"] == "not_found"
