"""All ``/api/*`` handlers (docs/WEBAPP.md §4).

Rules: every write goes through an ``app.services`` function inside ``write_tx`` and the
matching ``Notifier`` call runs after commit, outside the transaction, with the same
arguments as the bot handler. Booking state changes run under ``booking_lock`` with a
``before`` snapshot in the same transaction; a repeat is 200 with no second notification.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from app.bot.i18n import t
from app.config import CONSENT_VERSION
from app.db.enums import ACTIVE_STATUSES, Awaiting, BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import (
    barbers,
    bonus,
    bookings,
    catalog,
    clock,
    schedule,
    settings,
    slots,
    users,
    validation,
)
from app.services.dto import BookingDTO, ResolvedService, UserContext
from app.services.errors import (
    InputError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from app.webapp import serializers as ser
from app.webapp.auth import (
    AUTH_AGE_KEY,
    BOT_KEY,
    BOT_USERNAME_KEY,
    IDEMPOTENCY_KEY,
    INIT_RKEY,
    LIMITER_KEY,
    NOTIFIER_KEY,
    PHOTOS_KEY,
    SM_KEY,
    TOKEN_KEY,
    InitDataError,
    authed,
    client_ip,
    validate_contact_response,
)
from app.webapp.errors import ApiError
from app.webapp.locks import booking_lock, user_lock
from app.webapp.tasks import spawn

log = logging.getLogger("app.webapp")

routes = web.RouteTableDef()

BARBER_QUEUE_DAYS = 21  # today .. today + 20
PHOTOS_PER_MINUTE_PER_IP = 60
IDEMPOTENCY_KEY_MAX = 128
_QUEUE_STATUSES = frozenset(
    {BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW}
)
MAX_ID = 2**53  # larger JSON ints would overflow SQLite INTEGER (500) -> 400 instead


# ===================================================================== helpers


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise ApiError("invalid", 400, field="body") from None
    if not isinstance(data, dict):
        raise ApiError("invalid", 400, field="body")
    return data


def _opt_int(value: Any, field: str) -> int | None:
    """None / "" -> None; int or digit string -> int; else 400."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ApiError("invalid", 400, field=field)
    if isinstance(value, int):
        if value <= 0 or value > MAX_ID:
            raise ApiError("invalid", 400, field=field)
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 12:
        n = int(value)
        if n > 0:
            return n
    raise ApiError("invalid", 400, field=field)


def _path_id(request: web.Request) -> int:
    v = _opt_int(request.match_info.get("id"), "id")
    if v is None:
        raise ApiError("not_found", 404)
    return v


def parse_start_at(value: Any) -> datetime:
    """ISO-8601 with an offset or ``Z`` -> aware UTC; seconds must be zero [J-A4]."""
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ApiError("invalid", 400, field="start_at")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("invalid", 400, field="start_at") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ApiError("invalid", 400, field="start_at")
    if dt.second or dt.microsecond:
        raise ApiError("invalid", 400, field="start_at")
    return dt.astimezone(timezone.utc)


def _parse_day(value: Any, field: str = "day") -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ApiError("invalid", 400, field=field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError("invalid", 400, field=field) from None


def _with_user(ctx: UserContext, user: Any) -> UserContext:
    return dataclasses.replace(ctx, user=user)


def _notify(coro: Any) -> None:
    """Fire-and-forget (tracked, drained on stop). Notifier methods are @_guard-ed."""
    spawn(coro)


async def _shop(sm: Any) -> Any:
    async with read_tx(sm) as s:
        return await settings.get_shop_settings(s)


def _client_view(b: BookingDTO, shop: Any, now: datetime) -> dict[str, Any]:
    return ser.booking_json(b, viewer="client", shop=shop, now=now)


# ===================================================================== me


@routes.get("/api/me")
@authed(consent=False)
async def get_me(request: web.Request, ctx: UserContext) -> web.Response:
    data: dict[str, Any] = {
        "user": ser.user_json(ctx),
        "bot_username": request.app[BOT_USERNAME_KEY] or None,
        "webapp": {"auth_max_age": request.app[AUTH_AGE_KEY][0]},
    }
    init = request.get(INIT_RKEY)
    data["start_param"] = getattr(init, "start_param", None)
    # None = Telegram did not say (normal when opened from the bot chat) [finding #3]
    data["allows_write_to_bot"] = getattr(init, "allows_write_to_bot", None)
    if (ctx.user.consent_version or 0) < CONSENT_VERSION:
        data["consent_text"] = ser.plain(t("uz", "onb.consent"))
    return _json(data)


@routes.post("/api/me/consent")
@authed(consent=False)
async def post_consent(request: web.Request, ctx: UserContext) -> web.Response:
    await _body(request)
    async with write_tx(request.app[SM_KEY]) as s:
        u = await users.accept_consent(s, user_id=ctx.user.id, version=CONSENT_VERSION)
    bot = request.app.get(BOT_KEY)
    if bot is not None:
        # parity with onboarding.on_consent: reply-keyboard menu + per-chat commands
        _notify(_after_consent(bot, ctx.user.tg_id, u.lang or "uz", ctx.roles))
    return _json({"user": ser.user_json(_with_user(ctx, u))})


async def _after_consent(bot: Any, tg_id: int, lang: str, roles: Any) -> None:
    from app.bot import screen
    from app.bot.commands import set_user_commands
    from app.bot.keyboards.common import menu_for

    try:
        await screen.send_reply_kb(bot, tg_id, t(lang, "onb.thanks"), menu_for(roles, lang))
        await set_user_commands(bot, tg_id, lang, roles)
    except Exception:  # noqa: BLE001 - best effort; the chat may not exist yet
        log.debug("after-consent bot sync failed", exc_info=True)


@routes.post("/api/me/request-phone")
@authed()
async def post_request_phone(request: web.Request, ctx: UserContext) -> web.Response:
    """Fallback for clients without requestContact (or who declined it): put the bot's
    «📱 Raqamni yuborish» reply keyboard into the chat; webapp_contact saves the reply."""
    await _body(request)
    bot = request.app.get(BOT_KEY)
    sent = False
    if bot is not None:
        from app.bot import screen
        from app.bot.keyboards.common import contact_request

        lang = ctx.user.lang or "uz"
        msg = await screen.send_reply_kb(
            bot, ctx.user.tg_id, t(lang, "book.contact_prompt"), contact_request(lang)
        )
        sent = msg is not None
    return _json({"sent": sent})


@routes.post("/api/me/contact")
@authed()
async def post_contact(request: web.Request, ctx: UserContext) -> web.Response:
    body = await _body(request)
    response = body.get("response")
    if not isinstance(response, str) or not response:
        raise ApiError("invalid", 400, field="contact")
    try:
        contact = validate_contact_response(response, request.app[TOKEN_KEY])
    except InitDataError:
        raise ApiError("invalid", 400, field="contact") from None
    if contact.get("user_id") != ctx.user.tg_id:
        # same rule as the bot's contact.only_own
        raise ApiError("forbidden", 403, message=t("uz", "contact.only_own"))
    phone = contact.get("phone_number")
    if not isinstance(phone, str) or not phone.strip():
        raise ApiError("invalid", 400, field="contact")
    async with write_tx(request.app[SM_KEY]) as s:
        res = await users.set_verified_contact(s, user_id=ctx.user.id, raw_phone=phone)
    return _json({"user": ser.user_json(_with_user(ctx, res.user))})


@routes.post("/api/me/name")
@authed()
async def post_name(request: web.Request, ctx: UserContext) -> web.Response:
    body = await _body(request)
    name = body.get("name")
    if not isinstance(name, str):
        raise ApiError("invalid_name", 400, field="name")
    async with write_tx(request.app[SM_KEY]) as s:
        u = await users.set_full_name(s, user_id=ctx.user.id, name=name)
    return _json({"user": ser.user_json(_with_user(ctx, u))})


# ===================================================================== home / catalog


@routes.get("/api/home")
@authed()
async def get_home(request: web.Request, ctx: UserContext) -> web.Response:
    uid = ctx.user.id
    now = clock.now_utc()
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        week = await schedule.get_shop_week_hours(s)
        st = await bonus.get_bonus_status(s, uid, recent_limit=0)
        mine = await bookings.list_client_bookings(s, client_user_id=uid, past_limit=0, now=now)
        hint = await bookings.get_last_completed(s, client_user_id=uid)
    return _json(
        {
            "shop": ser.shop_json(shop, week),
            "user": ser.user_json(ctx),
            "bonus": ser.bonus_json(st, recent=False),
            "upcoming": _client_view(mine.upcoming[0], shop, now) if mine.upcoming else None,
            "upcoming_count": len(mine.upcoming),
            "rebook": (
                {
                    "barber_id": hint.barber_id,
                    "barber_name": hint.barber_name,
                    "service_id": hint.service_id,
                    "service_name": hint.service_name,
                }
                if hint
                else None
            ),
            "referral_enabled": bool(shop.referral_enabled and shop.loyalty_enabled),
            "is_barber": bool(ctx.roles.is_barber and ctx.roles.barber_id),
            "today": clock.local_today(now).isoformat(),
        }
    )


@routes.get("/api/barbers")
@authed()
async def get_barbers(request: web.Request, ctx: UserContext) -> web.Response:
    out = []
    async with read_tx(request.app[SM_KEY]) as s:
        for b in await barbers.list_barbers(s, public_only=True):
            offered = await catalog.list_barber_services(s, b.id)
            out.append(
                {
                    "id": b.id,
                    "name": b.display_name,
                    "bio": b.bio,
                    "photo_url": ser.photo_url(b),
                    "services": [
                        {
                            "service_id": rs.service_id,
                            "name": rs.name,
                            "duration_min": rs.duration_min,
                            "price_uzs": rs.price_uzs,
                        }
                        for rs in offered
                        if rs.service_id and rs.name
                    ],
                }
            )
    return _json({"barbers": out})


@routes.get("/api/barbers/{id}/photo")
async def get_barber_photo(request: web.Request) -> web.StreamResponse:
    """No auth: an <img> cannot send headers, and barber photos are public in the bot."""
    if not request.app[LIMITER_KEY].hit(("photo", client_ip(request)), PHOTOS_PER_MINUTE_PER_IP):
        raise ApiError("throttled", 429)
    bid = _path_id(request)
    try:
        async with read_tx(request.app[SM_KEY]) as s:
            b = await barbers.get_barber(s, bid)
    except NotFoundError:
        raise ApiError("not_found", 404) from None
    if not (b.is_active and b.is_public and b.photo_file_id):
        raise ApiError("not_found", 404)
    data = await request.app[PHOTOS_KEY].get(b.photo_file_id)
    if not data:
        raise ApiError("not_found", 404)
    return web.Response(
        body=data,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ===================================================================== booking wizard


@routes.get("/api/booking/services")
@authed()
async def get_booking_services(request: web.Request, ctx: UserContext) -> web.Response:
    async with read_tx(request.app[SM_KEY]) as s:
        has = await catalog.shop_has_services(s)
        items = await catalog.list_bookable_services(s) if has else []
    return _json(
        {
            "has_services": has,
            "services": [
                {
                    "id": x.id,
                    "name": x.name,
                    "duration_min": x.duration_min,
                    "price_uzs": x.price_uzs,
                }
                for x in items
            ],
        }
    )


async def _options(s: Any, service_id: int | None) -> list[tuple[Any, ResolvedService]]:
    """(barber, resolved price/duration) for the public barbers offering the service;
    barbers that raise InputError are skipped, exactly like the bot's _price_info."""
    out = []
    for b in await catalog.list_barbers_for_service(s, service_id=service_id):
        try:
            out.append((b, await catalog.resolve_service(s, barber_id=b.id, service_id=service_id)))
        except InputError:
            continue
    return out


def _lowest(options: list[tuple[Any, ResolvedService]]) -> tuple[ResolvedService | None, bool]:
    if not options:
        return None, False
    rs = [o[1] for o in options]
    low = min(rs, key=lambda r: (r.price_uzs, r.duration_min))
    return low, len({(r.price_uzs, r.duration_min) for r in rs}) > 1


@routes.get("/api/booking/barbers")
@authed()
async def get_booking_barbers(request: web.Request, ctx: UserContext) -> web.Response:
    service_id = _opt_int(request.query.get("service_id"), "service_id")
    async with read_tx(request.app[SM_KEY]) as s:
        options = await _options(s, service_id)
    low, varies = _lowest(options)
    return _json(
        {
            "any": {
                "available": len(options) >= 2,
                "price_uzs": low.price_uzs if low else 0,
                "price_varies": varies,
                "duration_min": low.duration_min if low else None,
            },
            "barbers": [
                {
                    "id": b.id,
                    "name": b.display_name,
                    "bio": b.bio,
                    "photo_url": ser.photo_url(b),
                    "price_uzs": rs.price_uzs,
                    "duration_min": rs.duration_min,
                }
                for b, rs in options
            ],
        }
    )


@routes.get("/api/booking/days")
@authed()
async def get_booking_days(request: web.Request, ctx: UserContext) -> web.Response:
    barber_id = _opt_int(request.query.get("barber_id"), "barber_id")
    service_id = _opt_int(request.query.get("service_id"), "service_id")
    now = clock.now_utc()
    async with read_tx(request.app[SM_KEY]) as s:
        days = await slots.get_available_days(
            s, barber_id=barber_id, service_id=service_id, now=now
        )
    today = clock.local_today(now)
    return _json({"today": today.isoformat(), "days": [ser.day_json(d, today) for d in days]})


async def _free(
    s: Any, barber_id: int | None, service_id: int | None, day: date, now: datetime
) -> list[datetime]:
    if barber_id:
        return await slots.get_free_slots(
            s, barber_id=barber_id, service_id=service_id, day=day, now=now
        )
    return await slots.get_free_slots_any(s, service_id=service_id, day=day, now=now)


@routes.get("/api/booking/slots")
@authed()
async def get_booking_slots(request: web.Request, ctx: UserContext) -> web.Response:
    barber_id = _opt_int(request.query.get("barber_id"), "barber_id")
    service_id = _opt_int(request.query.get("service_id"), "service_id")
    day = _parse_day(request.query.get("day"))
    now = clock.now_utc()
    today = clock.local_today(now)
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        if not today <= day <= today + timedelta(days=max(0, shop.booking_horizon_days - 1)):
            raise ApiError("invalid", 400, field="day")
        free = await _free(s, barber_id, service_id, day, now)
    return _json({"day": day.isoformat(), "slots": [ser.slot_json(x) for x in free]})


@routes.post("/api/booking/quote")
@authed()
async def post_quote(request: web.Request, ctx: UserContext) -> web.Response:
    body = await _body(request)
    barber_id = _opt_int(body.get("barber_id"), "barber_id")
    service_id = _opt_int(body.get("service_id"), "service_id")
    start_at = parse_start_at(body.get("start_at"))
    uid = ctx.user.id
    now = clock.now_utc()
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        barber = None
        price_varies = False
        if barber_id:
            barber = await barbers.get_barber(s, barber_id)
            rs: ResolvedService | None = await catalog.resolve_service(
                s, barber_id=barber_id, service_id=service_id
            )
            approval = not barber.auto_accept or not barber.is_reachable
        else:
            options = await _options(s, service_id)
            rs, price_varies = _lowest(options)
            approval = bool(options) and not all(
                b.auto_accept and b.is_reachable for b, _ in options
            )
        price = rs.price_uzs if rs else 0
        quote = await bonus.quote_redeem(s, user_id=uid, price_uzs=price) if price > 0 else None
        st = await bonus.get_bonus_status(s, uid, recent_limit=0) if quote else None
        soft = await users.is_soft_blocked(s, uid, now=now)
        fresh = await users.get_user(s, uid)
        is_new = (await users.get_client_stats(s, uid)).is_new
        local_day = clock.to_local(start_at).date()
        slot_free = start_at in await _free(s, barber_id, service_id, local_day, now)
    duration = rs.duration_min if rs else None
    end_at = start_at + timedelta(minutes=duration) if duration else None
    service_name = rs.name if rs else None
    return _json(
        {
            "barber": {"id": barber.id, "name": barber.display_name} if barber else None,
            "service": (
                {"id": rs.service_id, "name": service_name, "duration_min": duration}
                if rs and rs.service_id
                else None
            ),
            "start_at": ser.iso(start_at),
            "local": ser.local_json(start_at, end_at),
            "duration_min": duration,
            "price_uzs": price,
            "price_varies": price_varies,
            "bonus": (
                {
                    "possible": quote.possible,
                    "cost": quote.cost,
                    "final_price_uzs": quote.final_price_uzs,
                    "discount_uzs": quote.discount_uzs,
                    "mode": st.mode.value if st else None,
                    "available": st.available if st else None,
                }
                if quote is not None
                else None
            ),
            "soft_blocked": soft,
            "needs_approval_hint": bool(
                soft or approval or (is_new and shop.new_client_requires_approval)
            ),
            "slot_free": slot_free,
            "phone_ok": bool(fresh.phone and fresh.phone_verified),
            "name_ok": ser.name_ok(fresh.display_name),
            "user": {"display_name": fresh.display_name, "phone": fresh.phone},
            "address": shop.address or None,
        }
    )


@routes.post("/api/bookings")
@authed()
async def post_booking(request: web.Request, ctx: UserContext) -> web.Response:
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX:
        raise ApiError("invalid", 400, field="idempotency_key")
    tg_id = ctx.user.tg_id
    store = request.app[IDEMPOTENCY_KEY]
    async with user_lock(tg_id):
        hit = store.get(tg_id, key)
        if hit is not None:
            return _json(hit[1], status=hit[0])
        body = await _body(request)
        barber_id = _opt_int(body.get("barber_id"), "barber_id")
        service_id = _opt_int(body.get("service_id"), "service_id")
        start_at = parse_start_at(body.get("start_at"))
        raw_comment = body.get("comment")
        if raw_comment is not None and not isinstance(raw_comment, str):
            raise ApiError("invalid", 400, field="comment")
        comment = validation.validate_comment(raw_comment) if raw_comment is not None else None
        use_bonus = body.get("use_bonus") is True
        sm = request.app[SM_KEY]
        async with write_tx(sm) as s:
            res = await bookings.create_booking(
                s,
                client_user_id=ctx.user.id,
                barber_id=barber_id,
                service_id=service_id,
                start_at=start_at,
                comment=comment,
                use_bonus=use_bonus,
            )
        # inline, not a task: decision message ids exist before anyone can accept [J-D2]
        await request.app[NOTIFIER_KEY].booking_created(res)
        shop = await _shop(sm)
        data = {
            "booking": _client_view(res.booking, shop, clock.now_utc()),
            "auto_confirmed": res.auto_confirmed,
            "soft_blocked": res.client_soft_blocked,
            "address": shop.address or None,
        }
        store.put(tg_id, key, 201, data)
    return _json(data, status=201)


# ===================================================================== my bookings


@routes.get("/api/bookings")
@authed()
async def get_bookings(request: web.Request, ctx: UserContext) -> web.Response:
    now = clock.now_utc()
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        mine = await bookings.list_client_bookings(
            s, client_user_id=ctx.user.id, past_limit=20, now=now
        )
    return _json(
        {
            "upcoming": [_client_view(b, shop, now) for b in mine.upcoming],
            "past": [_client_view(b, shop, now) for b in mine.past],
        }
    )


@routes.get("/api/bookings/{id}")
@authed()
async def get_booking(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    uid = ctx.user.id
    now = clock.now_utc()
    async with read_tx(request.app[SM_KEY]) as s:
        b = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid, now=now)
        shop = await settings.get_shop_settings(s)
        if b.client_id == uid:
            return _json({"booking": _client_view(b, shop, now), "viewer": "client"})
        if not (ctx.roles.is_admin or (ctx.roles.barber_id and ctx.roles.barber_id == b.barber_id)):
            raise PermissionDeniedError("not allowed to view this booking")
        stats = await users.get_client_stats(s, b.client_id) if b.client_id else None
    return _json(
        {
            "booking": ser.booking_json(b, viewer="barber", shop=shop, now=now, stats=stats),
            "viewer": "barber",
        }
    )


@routes.post("/api/bookings/{id}/cancel")
@authed()
async def post_cancel(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    body = await _body(request)
    withdraw = body.get("withdraw") is True
    uid = ctx.user.id
    sm = request.app[SM_KEY]
    async with booking_lock(bid):
        async with write_tx(sm) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            if before.client_id != uid:
                raise PermissionDeniedError("not your booking")
            now = clock.now_utc()
            if before.status != BookingStatus.CANCELLED_BY_CLIENT:
                if withdraw:
                    ok = (
                        before.status == BookingStatus.PENDING
                        and before.awaiting == Awaiting.BARBER
                    )
                else:
                    ok = (
                        before.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED)
                        and before.start_at > now
                    )
                if not ok:
                    raise ApiError(
                        "invalid_transition",
                        409,
                        message=t("uz", "my.cannot_cancel"),
                        current=before.status.value,
                    )
            was_active = before.status in ACTIVE_STATUSES
            res = await bookings.cancel_by_client(
                s,
                booking_id=bid,
                client_user_id=uid,
                reason="withdrawn" if withdraw else None,
            )
            shop = await settings.get_shop_settings(s)
    if was_active and res.booking.status != before.status:
        _notify(request.app[NOTIFIER_KEY].booking_cancelled(res.booking, by_client=True))
    return _json({"booking": _client_view(res.booking, shop, clock.now_utc()), "late": res.late})


@routes.post("/api/bookings/{id}/proposal")
@authed()
async def post_proposal(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    body = await _body(request)
    accept = body.get("accept")
    if not isinstance(accept, bool):
        raise ApiError("invalid", 400, field="accept")
    uid = ctx.user.id
    sm = request.app[SM_KEY]
    async with booking_lock(bid):
        async with write_tx(sm) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            if before.client_id != uid:
                raise PermissionDeniedError("not your booking")
            was_open = before.status == BookingStatus.PENDING and before.awaiting == Awaiting.CLIENT
            nb = await bookings.answer_proposal(
                s, booking_id=bid, client_user_id=uid, accept=accept
            )
            shop = await settings.get_shop_settings(s)
    if was_open and nb.status != before.status:
        _notify(request.app[NOTIFIER_KEY].proposal_answered(nb))
    return _json({"booking": _client_view(nb, shop, clock.now_utc())})


# ===================================================================== bonus


@routes.get("/api/bonus")
@authed()
async def get_bonus(request: web.Request, ctx: UserContext) -> web.Response:
    async with read_tx(request.app[SM_KEY]) as s:
        st = await bonus.get_bonus_status(s, ctx.user.id, recent_limit=50)
        shop = await settings.get_shop_settings(s)
    username = request.app[BOT_USERNAME_KEY]
    enabled = bool(shop.referral_enabled and shop.loyalty_enabled)
    return _json(
        {
            "bonus": ser.bonus_json(st),
            "referral": {
                "enabled": enabled,
                "bonus": shop.referral_bonus,
                "link": (
                    f"https://t.me/{username}?start=ref_{ctx.user.ref_code}"
                    if enabled and username
                    else None
                ),
            },
        }
    )


# ===================================================================== barber panel


@routes.get("/api/barber/days")
@authed(barber=True)
async def get_barber_days(request: web.Request, ctx: UserContext) -> web.Response:
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        b = await barbers.get_barber(s, ctx.roles.barber_id)  # type: ignore[arg-type]
    today = clock.local_today()
    n = min(max(7, int(shop.booking_horizon_days)), BARBER_QUEUE_DAYS)
    return _json(
        {
            "barber": {"id": b.id, "name": b.display_name},
            "today": today.isoformat(),
            "days": [ser.day_info(today + timedelta(days=i), today) for i in range(n)],
            "bot_username": request.app[BOT_USERNAME_KEY] or None,
        }
    )


@routes.get("/api/barber/queue")
@authed(barber=True)
async def get_barber_queue(request: web.Request, ctx: UserContext) -> web.Response:
    now = clock.now_utc()
    today = clock.local_today(now)
    raw = request.query.get("day")
    day = _parse_day(raw) if raw else today
    if not today <= day <= today + timedelta(days=BARBER_QUEUE_DAYS - 1):
        raise ApiError("invalid", 400, field="day")
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        items = await bookings.list_barber_day(
            s,
            actor_user_id=ctx.user.id,
            barber_id=ctx.roles.barber_id,  # type: ignore[arg-type]
            day=day,
            include_inactive=True,
            now=now,
        )
        # the default (active) statuses + COMPLETED/NO_SHOW history + EXPIRED bookings that
        # can still be late-accepted, like the bot's decision message allows [finding #6]
        items = [
            b
            for b in items
            if b.status in _QUEUE_STATUSES
            or (b.status == BookingStatus.EXPIRED and ser.barber_actions(b, shop, now)["accept"])
        ]
        items = sorted(items, key=lambda b: (b.start_at, b.id))
        stats: dict[int, Any] = {}
        for b in items:
            if b.client_id and b.client_id not in stats:
                stats[b.client_id] = await users.get_client_stats(s, b.client_id)
    return _json(
        {
            "today": today.isoformat(),
            "day": ser.day_info(day, today),
            "bookings": [
                ser.booking_json(
                    b, viewer="barber", shop=shop, now=now, stats=stats.get(b.client_id or 0)
                )
                for b in items
            ],
            "pending_count": sum(
                1
                for b in items
                if b.status == BookingStatus.PENDING and b.awaiting == Awaiting.BARBER
            ),
        }
    )


async def _barber_view(request: web.Request, ctx: UserContext, b: BookingDTO) -> dict[str, Any]:
    async with read_tx(request.app[SM_KEY]) as s:
        shop = await settings.get_shop_settings(s)
        stats = await users.get_client_stats(s, b.client_id) if b.client_id else None
    return ser.booking_json(b, viewer="barber", shop=shop, now=clock.now_utc(), stats=stats)


def _own(ctx: UserContext, b: BookingDTO) -> None:
    """The barber panel acts on the barber's own bookings; admins keep the bot's reach."""
    if b.barber_id != ctx.roles.barber_id and not ctx.roles.is_admin:
        raise PermissionDeniedError("not your booking")


@routes.post("/api/barber/bookings/{id}/accept")
@authed(barber=True)
async def post_accept(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    await _body(request)
    uid = ctx.user.id
    async with booking_lock(bid):
        async with write_tx(request.app[SM_KEY]) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            _own(ctx, before)
            b = await bookings.confirm_booking(s, booking_id=bid, actor_user_id=uid)
    if before.status != BookingStatus.CONFIRMED and b.status == BookingStatus.CONFIRMED:
        # no skip: every Telegram copy of the decision message is edited to the outcome
        _notify(request.app[NOTIFIER_KEY].booking_confirmed(b))
    return _json({"booking": await _barber_view(request, ctx, b)})


REJECT_CODES = ("busy", "sick")


@routes.post("/api/barber/bookings/{id}/reject")
@authed(barber=True)
async def post_reject(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    body = await _body(request)
    raw = body.get("reason")
    reason: str | None
    if raw is None or raw == "none":
        reason = None
    elif isinstance(raw, str) and raw in REJECT_CODES:
        reason = raw
    elif isinstance(raw, str):
        reason = (validation.validate_comment(raw) or "")[:200]
        if not reason:
            raise ApiError("invalid", 400, field="reason")
    else:
        raise ApiError("invalid", 400, field="reason")
    uid = ctx.user.id
    async with booking_lock(bid):
        async with write_tx(request.app[SM_KEY]) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            _own(ctx, before)
            if before.status == BookingStatus.REJECTED:
                b = before
            else:
                if not (
                    before.status == BookingStatus.PENDING and before.awaiting == Awaiting.BARBER
                ):
                    raise InvalidTransitionError("not pending", current=before.status.value)
                b = await bookings.reject_booking(
                    s, booking_id=bid, actor_user_id=uid, reason=reason
                )
    if before.status != BookingStatus.REJECTED and b.status == BookingStatus.REJECTED:
        _notify(request.app[NOTIFIER_KEY].booking_rejected(b))
    return _json({"booking": await _barber_view(request, ctx, b)})


@routes.post("/api/barber/bookings/{id}/done")
@authed(barber=True)
async def post_done(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    await _body(request)
    uid = ctx.user.id
    async with booking_lock(bid):
        async with write_tx(request.app[SM_KEY]) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            _own(ctx, before)
            r = await bookings.complete_booking(s, booking_id=bid, actor_user_id=uid)
    notes: list[str] = []
    if before.status != BookingStatus.COMPLETED and r.booking.status == BookingStatus.COMPLETED:
        notes.append(t("uz", "barber.done_ok"))
        if r.bonus_failed:
            notes.append(t("uz", "barber.bonus_failed", id=bid))
        elif r.redeemed:
            notes.append(t("uz", "barber.bonus_applied"))
        if r.needs_visit_confirm:
            notes.append(t("uz", "barber.visit_pending"))
        _notify(request.app[NOTIFIER_KEY].booking_completed(r))
    return _json(
        {
            "booking": await _barber_view(request, ctx, r.booking),
            "notes": [ser.plain(n) for n in notes],
        }
    )


@routes.post("/api/barber/bookings/{id}/no-show")
@authed(barber=True)
async def post_no_show(request: web.Request, ctx: UserContext) -> web.Response:
    bid = _path_id(request)
    await _body(request)
    uid = ctx.user.id
    async with booking_lock(bid):
        async with write_tx(request.app[SM_KEY]) as s:
            before = await bookings.get_booking_for_actor(s, booking_id=bid, actor_user_id=uid)
            _own(ctx, before)
            r = await bookings.mark_no_show(s, booking_id=bid, actor_user_id=uid)
    notes: list[str] = []
    if before.status != BookingStatus.NO_SHOW and r.booking.status == BookingStatus.NO_SHOW:
        notes.append(t("uz", "barber.no_show_ok"))
        if r.reversed:
            notes.append(t("uz", "barber.reversed", n=r.reversed))
        _notify(request.app[NOTIFIER_KEY].booking_no_show(r))
    return _json(
        {
            "booking": await _barber_view(request, ctx, r.booking),
            "notes": [ser.plain(n) for n in notes],
        }
    )
