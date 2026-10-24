"""Shared helpers for the Mini App tests: signed initData with a dummy token, a recording
notifier, a pinned clock and an in-process aiohttp client (docs/WEBAPP.md §7)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db.uow import read_tx
from app.services import clock, users
from app.webapp.auth import build_init_data
from app.webapp.server import create_app

TOKEN = "123456789:TEST-dummy-token-for-tests-only"
BOT_USERNAME = "test_bot"


def init_data(
    tg_id: int,
    *,
    first_name: str = "Test",
    username: str | None = None,
    start_param: str | None = None,
    auth_date: int | None = None,
    token: str = TOKEN,
    user: dict[str, Any] | None | bool = True,
    extra: dict[str, str] | None = None,
) -> str:
    pairs: dict[str, str] = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
    }
    if user is True:
        u: dict[str, Any] = {"id": tg_id, "first_name": first_name, "language_code": "uz"}
        if username:
            u["username"] = username
        pairs["user"] = json.dumps(u, separators=(",", ":"))
    elif isinstance(user, dict):
        pairs["user"] = json.dumps(user)
    if start_param:
        pairs["start_param"] = start_param
    if extra:
        pairs.update(extra)
    return build_init_data(pairs, token)


def contact_response(
    tg_id: int, phone: str, *, token: str = TOKEN, owner: int | None = None
) -> str:
    contact = {
        "user_id": owner if owner is not None else tg_id,
        "phone_number": phone,
        "first_name": "X",
    }
    return build_init_data(
        {"contact": json.dumps(contact), "auth_date": str(int(time.time()))}, token
    )


class FakeNotifier:
    """Records every call; same method names as app.bot.notifier.Notifier."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start: Any) -> None:
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


class Api:
    """Thin wrapper: ``await api.get(tg, "/api/me")`` -> (status, json)."""

    def __init__(self, client: TestClient, notifier: Any) -> None:
        self.client = client
        self.notifier = notifier

    async def req(
        self,
        method: str,
        path: str,
        tg: int | None,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        auth: str | None = None,
        **init_kw: Any,
    ) -> tuple[int, Any]:
        h = dict(headers or {})
        if auth is not None:
            h["Authorization"] = auth
        elif tg is not None:
            h["Authorization"] = "tma " + init_data(tg, **init_kw)
        kw: dict[str, Any] = {"headers": h}
        if body is not None:
            kw["json"] = body
        async with self.client.request(method, path, **kw) as r:
            try:
                data = await r.json()
            except Exception:  # noqa: BLE001
                data = await r.text()
            self.last_headers = r.headers
            return r.status, data

    async def get(self, tg: int | None, path: str, **kw: Any) -> tuple[int, Any]:
        return await self.req("GET", path, tg, **kw)

    async def post(self, tg: int | None, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return await self.req("POST", path, tg, {} if body is None else body, **kw)


async def make_api(sm: Any, notifier: Any = None, **app_kw: Any) -> Api:
    notifier = notifier if notifier is not None else FakeNotifier()
    app = create_app(
        sm=sm,
        bot=app_kw.pop("bot", None),
        notifier=notifier,
        bot_token=TOKEN,
        bot_username=BOT_USERNAME,
        **app_kw,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return Api(client, notifier)


async def tg_of(sm: Any, uid: int) -> int:
    async with read_tx(sm) as s:
        return (await users.get_user(s, uid)).tg_id
