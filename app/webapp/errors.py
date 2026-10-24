"""ServiceError / ApiError -> HTTP status + JSON body (docs/WEBAPP.md §4.1).

Body shape: ``{"error": {"code", "message", "ctx"?, "reason"?, "field"?}}``. ``ctx`` holds
JSON scalars only. Never logs headers or initData: only method, path and tg_id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from aiohttp import web

from app.bot.i18n import t
from app.bot.views import err_text
from app.services.errors import ServiceError

log = logging.getLogger("app.webapp")

STATUS: dict[str, int] = {
    "unauthorized": 401,
    "user_blocked": 403,
    "consent_required": 403,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "too_large": 413,
    "invalid": 400,
    "invalid_phone": 400,
    "invalid_name": 400,
    "phone_required": 422,
    "slot_taken": 409,
    "slot_unavailable": 409,
    "barber_inactive": 409,
    "client_limit": 409,
    "invalid_transition": 409,
    "too_early": 409,
    "too_late": 409,
    "no_bonus": 409,
    "proposal_limit": 409,
    "throttled": 429,
}

# web-only texts (Uzbek UI); service errors use the bot's own err.* strings
_WEB_MESSAGES: dict[str, str] = {
    "unauthorized": "Sessiya eskirdi, ilovani qayta oching.",
    "throttled": "Biroz kuting va qayta urinib ko'ring.",
    "method_not_allowed": "Bu amal qo'llab-quvvatlanmaydi.",
    "too_large": "So'rov juda katta.",
}


class ApiError(Exception):
    """A web-only error: ``ApiError("invalid", 400, field="day")``."""

    def __init__(self, code: str, status: int | None = None, message: str = "", **ctx: object):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS.get(code, 400)
        self.message = message
        self.ctx: dict[str, object] = dict(ctx)


def _scalar(v: object) -> object | None:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, date):
        return v.isoformat()
    return _DROP


_DROP = object()


def ctx_json(ctx: dict[str, object] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in (ctx or {}).items():
        sv = _scalar(v)
        if sv is not _DROP:
            out[str(k)] = sv
    return out


def _message(code: str, ctx: dict[str, object], explicit: str = "") -> str:
    if explicit:
        return explicit
    if code in _WEB_MESSAGES:
        return _WEB_MESSAGES[code]

    class _E:  # err_text only reads .code and .ctx
        pass

    e = _E()
    e.code = code  # type: ignore[attr-defined]
    e.ctx = ctx  # type: ignore[attr-defined]
    return err_text("uz", e)  # type: ignore[arg-type]


def error_payload(code: str, message: str = "", ctx: dict[str, object] | None = None) -> dict:
    c = ctx_json(ctx)
    body: dict[str, Any] = {"code": code, "message": _message(code, c, message)}
    if c:
        body["ctx"] = c
        for k in ("reason", "field"):
            if k in c:
                body[k] = c[k]
    return {"error": body}


def json_error(
    code: str, status: int | None = None, message: str = "", **ctx: object
) -> web.Response:
    return web.json_response(
        error_payload(code, message, ctx), status=status or STATUS.get(code, 400)
    )


def error_response(e: ServiceError) -> web.Response:
    code = getattr(e, "code", "error") or "error"
    status = STATUS.get(code, 400)
    return web.json_response(error_payload(code, "", dict(e.ctx)), status=status)


def api_error_response(e: ApiError) -> web.Response:
    return web.json_response(error_payload(e.code, e.message, e.ctx), status=e.status)


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _tg_key() -> Any:
    from app.webapp.auth import TG_ID_RKEY

    return TG_ID_RKEY


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as e:
        return api_error_response(e)
    except ServiceError as e:
        return error_response(e)
    except web.HTTPException as e:
        if not request.path.startswith("/api") or e.status < 400:
            raise
        code = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(
            e.status, "invalid"
        )
        return json_error(code, e.status)
    except Exception:
        log.exception(
            "webapp %s %s failed (tg_id=%s)", request.method, request.path, request.get(_tg_key())
        )
        return web.json_response(
            {"error": {"code": "error", "message": t("uz", "err.error")}}, status=500
        )
