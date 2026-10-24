"""Telegram WebApp initData validation, user resolution and the ``@authed`` gate
(docs/WEBAPP.md §3).

Never log ``initData``, the hash or request headers.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import CONSENT_VERSION
from app.db.uow import read_tx, write_tx
from app.services import barbers, users
from app.services.dto import UserContext
from app.webapp.errors import ApiError
from app.webapp.ratelimit import RateLimiter

if TYPE_CHECKING:
    from aiogram import Bot

    from app.bot.notifier import Notifier
    from app.webapp.locks import IdempotencyStore
    from app.webapp.photos import PhotoCache

# typed application keys (re-exported by server.py)
SM_KEY: web.AppKey[async_sessionmaker[AsyncSession]] = web.AppKey("sm")
BOT_KEY: web.AppKey[Bot | None] = web.AppKey("bot")
NOTIFIER_KEY: web.AppKey[Notifier] = web.AppKey("notifier")
BOT_USERNAME_KEY: web.AppKey[str] = web.AppKey("bot_username")
TOKEN_KEY: web.AppKey[str] = web.AppKey("bot_token")
PHOTOS_KEY: web.AppKey[PhotoCache] = web.AppKey("photos")
LIMITER_KEY: web.AppKey[RateLimiter] = web.AppKey("limiter")
IDEMPOTENCY_KEY: web.AppKey[IdempotencyStore] = web.AppKey("idempotency")
AUTH_AGE_KEY: web.AppKey[tuple[int, int]] = web.AppKey("auth_age")  # (GET, POST) seconds
LIMITS_KEY: web.AppKey[dict[str, int]] = web.AppKey("limits")
TRUST_PROXY_KEY: web.AppKey[bool] = web.AppKey("trust_proxy")

# per-request storage keys (web.RequestKey on aiohttp >= 3.13; plain strings before)
_RequestKey: Any = getattr(web, "RequestKey", None)
TG_ID_RKEY: Any = _RequestKey("tg_id", int) if _RequestKey else "tg_id"
INIT_RKEY: Any = _RequestKey("init", object) if _RequestKey else "init"

MAX_INIT_DATA = 4096
FUTURE_SKEW = 60
CONTACT_MAX_AGE = 600

READS_PER_MINUTE = 120
FAILS_PER_MINUTE_PER_IP = 60


class InitDataError(Exception):
    """reason: bad_hash | expired | bad_init_data | no_user | missing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WebAppInitData:
    user_id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None
    auth_date: int
    start_param: str | None
    allows_write_to_bot: bool | None = None


# ----------------------------------------------------------------- signature


def _secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _check_string(pairs: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))


def sign(pairs: dict[str, str], bot_token: str) -> str:
    """Hex HMAC of the pairs (without ``hash``), exactly as Telegram computes it."""
    return hmac.new(_secret(bot_token), _check_string(pairs).encode(), hashlib.sha256).hexdigest()


def build_init_data(pairs: dict[str, str], bot_token: str) -> str:
    """Signed query string; used by tests and local tooling (never with the real token in code)."""
    data = dict(pairs)
    data["hash"] = sign(pairs, bot_token)
    return urlencode(data)


def verify_signed(
    data: str, bot_token: str, *, max_age: int, now: float | None = None
) -> dict[str, str]:
    """Parse + HMAC + auth_date freshness for initData and requestContact responses."""
    if not data:
        raise InitDataError("missing")
    if len(data.encode()) > MAX_INIT_DATA:
        raise InitDataError("bad_init_data")
    try:
        items = parse_qsl(data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("bad_init_data") from None
    pairs: dict[str, str] = {}
    for k, v in items:
        if k in pairs:
            raise InitDataError("bad_init_data")
        pairs[k] = v
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("bad_init_data")
    if not hmac.compare_digest(sign(pairs, bot_token), received.lower()):
        raise InitDataError("bad_hash")
    try:
        auth_date = int(pairs.get("auth_date", ""))
    except ValueError:
        raise InitDataError("bad_init_data") from None
    now = time.time() if now is None else now
    if now - auth_date > max_age:
        raise InitDataError("expired")
    if auth_date - now > FUTURE_SKEW:
        raise InitDataError("bad_init_data")
    return pairs


def validate_init_data(
    init_data: str, bot_token: str, *, max_age: int, now: float | None = None
) -> WebAppInitData:
    pairs = verify_signed(init_data, bot_token, max_age=max_age, now=now)
    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("no_user")
    try:
        u = json.loads(raw_user)
    except ValueError:
        raise InitDataError("bad_init_data") from None
    if not isinstance(u, dict):
        raise InitDataError("no_user")
    uid = u.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise InitDataError("no_user")
    first = u.get("first_name")
    if not isinstance(first, str):
        raise InitDataError("no_user")

    def _opt(key: str) -> str | None:
        v = u.get(key)
        return v if isinstance(v, str) and v else None

    return WebAppInitData(
        user_id=uid,
        first_name=first,
        last_name=_opt("last_name"),
        username=_opt("username"),
        language_code=_opt("language_code"),
        auth_date=int(pairs["auth_date"]),
        start_param=pairs.get("start_param") or None,
        allows_write_to_bot=(
            u["allows_write_to_bot"] if isinstance(u.get("allows_write_to_bot"), bool) else None
        ),
    )


def validate_contact_response(
    response: str, bot_token: str, *, now: float | None = None
) -> dict[str, Any]:
    """The ``requestContact`` callback ``response``: same HMAC, 10 min max age.

    Returns the decoded ``contact`` object. Raises InitDataError.
    """
    pairs = verify_signed(response, bot_token, max_age=CONTACT_MAX_AGE, now=now)
    try:
        contact = json.loads(pairs.get("contact") or "")
    except ValueError:
        raise InitDataError("bad_init_data") from None
    if not isinstance(contact, dict):
        raise InitDataError("bad_init_data")
    return contact


# ----------------------------------------------------------------- users


async def resolve_user(sm: async_sessionmaker[AsyncSession], init: WebAppInitData) -> UserContext:
    """Mirror of UserMiddleware: read first, write only for new/changed users [J-A5]."""
    async with read_tx(sm) as s:
        ctx = await users.load_context(s, tg_id=init.user_id)
    if ctx is None or users.needs_sync(
        ctx.user, tg_username=init.username, first_name=init.first_name or ""
    ):
        async with write_tx(sm) as s:
            ctx = await users.ensure_user(
                s,
                tg_id=init.user_id,
                tg_username=init.username,
                first_name=init.first_name or "",
                language_code=init.language_code,
                start_payload=init.start_param,
            )
    if ctx.roles.is_barber and ctx.barber_reachable is False:
        async with write_tx(sm) as s:
            await barbers.mark_reachable_by_user(s, user_id=ctx.user.id)
    return ctx


# ----------------------------------------------------------------- gate


def client_ip(request: web.Request) -> str:
    """Peer address, or with WEBAPP_TRUST_PROXY the RIGHTMOST X-Forwarded-For entry:
    the one appended by our own (single) proxy. The leftmost entry is client-controlled
    when the proxy appends (nginx $proxy_add_x_forwarded_for, Cloudflare)."""
    if request.app.get(TRUST_PROXY_KEY):
        fwd = request.headers.get("X-Forwarded-For", "")
        last = fwd.split(",")[-1].strip()
        if last:
            return last
    return request.remote or "?"


def _unauthorized(request: web.Request, reason: str) -> ApiError:
    limiter = request.app[LIMITER_KEY]
    if not limiter.hit(("ip401", client_ip(request)), FAILS_PER_MINUTE_PER_IP):
        return ApiError("throttled", 429)
    return ApiError("unauthorized", 401, reason=reason)


def init_data_from(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "tma" or not value.strip():
        return ""
    return value.strip()


Handler = Callable[[web.Request, UserContext], Awaitable[web.StreamResponse]]


def authed(
    *, consent: bool = True, barber: bool = False
) -> Callable[[Handler], Callable[[web.Request], Awaitable[web.StreamResponse]]]:
    """Order [J-A6]: signature -> per-tg_id limit (before any DB access) -> user ->
    blocked -> consent -> barber role -> JSON content type on POST."""

    def deco(fn: Handler) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @functools.wraps(fn)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            app = request.app
            write = request.method not in ("GET", "HEAD")
            raw = init_data_from(request)
            if not raw:
                raise _unauthorized(request, "missing")
            max_get, max_post = app[AUTH_AGE_KEY]
            try:
                init = validate_init_data(
                    raw, app[TOKEN_KEY], max_age=max_post if write else max_get
                )
            except InitDataError as e:
                raise _unauthorized(request, e.reason) from None
            request[TG_ID_RKEY] = init.user_id

            limits = app[LIMITS_KEY]
            key = ("w" if write else "r", init.user_id)
            if not app[LIMITER_KEY].hit(key, limits["write" if write else "read"]):
                raise ApiError("throttled", 429)

            ctx = await resolve_user(app[SM_KEY], init)
            if ctx.user.is_blocked:
                raise ApiError("user_blocked", 403)
            if consent and (ctx.user.consent_version or 0) < CONSENT_VERSION:
                raise ApiError("consent_required", 403)
            if barber and not (ctx.roles.is_barber and ctx.roles.barber_id):
                raise ApiError("forbidden", 403)
            if write and request.content_type != "application/json":
                raise ApiError("invalid", 400, field="body")
            request[INIT_RKEY] = init
            return await fn(request, ctx)

        return wrapper

    return deco
