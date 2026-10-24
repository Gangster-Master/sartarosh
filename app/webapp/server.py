"""aiohttp application factory and lifecycle (docs/WEBAPP.md §2.1).

One process, one event loop: the server runs next to aiogram polling in
``app/bot/main.py``. ``RUNNING`` tells ``views.hub_content`` whether to show the
"🌐 Ilovani ochish" button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from app.config import get_config
from app.webapp import api
from app.webapp.auth import (
    AUTH_AGE_KEY,
    BOT_KEY,
    BOT_USERNAME_KEY,
    IDEMPOTENCY_KEY,
    LIMITER_KEY,
    LIMITS_KEY,
    NOTIFIER_KEY,
    PHOTOS_KEY,
    READS_PER_MINUTE,
    SM_KEY,
    TOKEN_KEY,
    TRUST_PROXY_KEY,
)
from app.webapp.errors import error_middleware
from app.webapp.locks import IdempotencyStore
from app.webapp.photos import PhotoCache
from app.webapp.ratelimit import RateLimiter
from app.webapp.tasks import drain_pending_notifications

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.bot.notifier import Notifier

__all__ = [
    "AUTH_AGE_KEY",
    "BOT_KEY",
    "BOT_USERNAME_KEY",
    "LIMITER_KEY",
    "NOTIFIER_KEY",
    "PHOTOS_KEY",
    "RUNNING",
    "SM_KEY",
    "TOKEN_KEY",
    "WebAppHandle",
    "create_app",
    "start_webapp",
]

log = logging.getLogger("app.webapp")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CLIENT_MAX_SIZE = 64 * 1024

RUNNING: bool = False  # set by start_webapp, cleared by WebAppHandle.stop

CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """on_response_prepare: runs for every response, errors and static files included.
    No X-Frame-Options (Telegram Web embeds the app in an iframe) and no CORS headers."""
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["Content-Security-Policy"] = CSP
    for k in [k for k in h if k.lower().startswith("access-control-")]:
        del h[k]
    path = request.path
    if path == "/" or path == "/index.html" or path.startswith("/static/"):
        h["Cache-Control"] = "no-cache"
    elif path.startswith("/api/") and "Cache-Control" not in h:
        h["Cache-Control"] = "no-store"


async def index(request: web.Request) -> web.StreamResponse:
    f = STATIC_DIR / "index.html"
    if not f.is_file():
        raise web.HTTPNotFound(text="index.html is missing")
    return web.FileResponse(f)


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(
    *,
    sm: async_sessionmaker[AsyncSession],
    bot: Bot | None,
    notifier: Notifier | Any,
    bot_token: str,
    bot_username: str,
    auth_max_age: int | None = None,
    auth_max_age_write: int | None = None,
    write_per_minute: int | None = None,
    read_per_minute: int | None = None,
    trust_proxy: bool | None = None,
    static_dir: Path | None = None,
) -> web.Application:
    cfg = get_config()
    app = web.Application(middlewares=[error_middleware], client_max_size=CLIENT_MAX_SIZE)
    app[SM_KEY] = sm
    app[BOT_KEY] = bot
    app[NOTIFIER_KEY] = notifier
    app[TOKEN_KEY] = bot_token
    app[BOT_USERNAME_KEY] = bot_username or ""
    app[PHOTOS_KEY] = PhotoCache(bot)
    app[LIMITER_KEY] = RateLimiter()
    app[IDEMPOTENCY_KEY] = IdempotencyStore()
    app[AUTH_AGE_KEY] = (
        auth_max_age if auth_max_age is not None else cfg.webapp_auth_max_age,
        auth_max_age_write if auth_max_age_write is not None else cfg.webapp_auth_max_age_write,
    )
    app[LIMITS_KEY] = {
        "read": read_per_minute or READS_PER_MINUTE,
        "write": write_per_minute or cfg.throttle_per_minute,
    }
    app[TRUST_PROXY_KEY] = cfg.webapp_trust_proxy if trust_proxy is None else trust_proxy
    app.on_response_prepare.append(security_headers)

    app.router.add_routes(api.routes)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    static = static_dir or STATIC_DIR
    if static.is_dir():
        app.router.add_static("/static/", static, show_index=False, follow_symlinks=False)
    else:
        log.warning("webapp static directory %s is missing; only /api is served", static)
    return app


@dataclass
class WebAppHandle:
    runner: web.AppRunner

    async def stop(self) -> None:
        global RUNNING
        RUNNING = False
        await self.runner.cleanup()
        await drain_pending_notifications(timeout=10)


async def start_webapp(app: web.Application, *, host: str, port: int) -> WebAppHandle:
    """Bind and serve. Raises OSError (port in use etc.); the bot keeps working then."""
    global RUNNING
    runner = web.AppRunner(app, access_log=None)  # initData travels in headers: no access log
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    RUNNING = True
    public = get_config().webapp_public_url
    log.info(
        "webapp listening on http://%s:%s (%s)",
        host,
        port,
        f"public URL: {public}" if public else "buttons hidden",
    )
    return WebAppHandle(runner)
