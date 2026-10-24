# Telegram Mini App ("web view"): spec

**Status:** CTO spec, binding for this feature. **Final contract after review:** where the body and §10 "Judge rulings" disagree, §10 wins. It extends `docs/ARCHITECTURE.md`. Every rule there still holds: services are aiogram-free, there is no network I/O inside a transaction, permissions are checked in services, and datetimes are aware UTC at the boundary.

**Goal:** a Telegram Mini App that runs in the same Python process as the bot. It lets clients book, view and cancel bookings, see bonuses and browse barbers. It lets barbers work their queue. Every write goes through the existing `app/services` functions and is followed by the same `Notifier` call the bot handler makes. A booking made in the web app and a booking made in the bot must look the same to the barber, to the database and to the scheduler.

**Ownership**

| Path | Owner |
|---|---|
| `app/webapp/__init__.py`, `auth.py`, `api.py`, `server.py`, `serializers.py`, `errors.py`, `photos.py`, `locks.py`, `tasks.py`, `ratelimit.py` | BACKEND |
| `app/config.py` (new WEBAPP_* fields), `app/bot/main.py` wiring, `app/bot/menu_button.py`, `app/bot/handlers/webapp_contact.py`, the hub button in `app/bot/views.py`, new locale keys in `uz.py`/`ru.py` | BACKEND |
| `README.md` section, `.env.example`, `docker-compose.yml`, `Dockerfile` `EXPOSE` | BACKEND |
| `app/webapp/static/**` | FRONTEND |
| `tests/test_webapp_*.py` | QA (BACKEND writes the first versions) |

Import rules for `app/webapp/*.py`:
- **May import:** `aiohttp`, `aiogram` (only `Bot`, `aiogram.types`, `aiogram.utils.web_app`), `app.config`, `app.services.*`, `app.services.dto`, `app.services.errors`, `app.db.enums`, `app.db.uow`, `app.bot.notifier.Notifier` (type only), `app.bot.i18n` (`t`, `has_key`) and `app.bot.views.err_text`.
- **Must not import:** `app.db.models`, `app.bot.handlers.*`, any internal service function (marked "Internal" in the docstrings and in ARCHITECTURE §4), and must not build SQL.

---

## 1. Configuration (`app/config.py`)

Add these fields to `Config`:

```python
webapp_enabled: bool = False          # WEBAPP_ENABLED
webapp_host: str = "127.0.0.1"        # WEBAPP_HOST (compose sets 0.0.0.0) [J-B7]
webapp_port: int = 8080               # WEBAPP_PORT
webapp_url: str = ""                  # WEBAPP_URL, public https URL of index.html, e.g. https://shop.example.uz/
webapp_auth_max_age: int = 86400      # WEBAPP_AUTH_MAX_AGE, seconds; GET requests [J-B1]
webapp_auth_max_age_write: int = 86400 # WEBAPP_AUTH_MAX_AGE_WRITE, seconds; POST requests [J-B1]
webapp_trust_proxy: bool = False      # WEBAPP_TRUST_PROXY; use X-Forwarded-For for per-IP limits [J-B5]

@property
def webapp_public_url(self) -> str | None:
    """WEBAPP_URL if the web app is enabled and the URL starts with https://, else None."""
```

Rules:
- If `webapp_enabled` is false, no server starts and no buttons are shown.
- If `webapp_enabled` is true and `WEBAPP_URL` is empty, the server still starts (useful for local tests), but the menu button and the hub button are hidden.
- If `WEBAPP_URL` is set but does not start with `https://`, log a warning and treat it as empty. Telegram only opens https Mini Apps.

`.env.example` additions:

```
# --- Telegram Mini App (web view) ---
#WEBAPP_ENABLED=false
#WEBAPP_HOST=127.0.0.1
#WEBAPP_PORT=8080
# Public HTTPS URL that serves the app (reverse proxy or cloudflared tunnel). Empty = buttons hidden.
#WEBAPP_URL=
#WEBAPP_AUTH_MAX_AGE=86400
#WEBAPP_AUTH_MAX_AGE_WRITE=86400
#WEBAPP_TRUST_PROXY=false
```

---

## 2. Module layout

```
app/webapp/
├── __init__.py        # empty (docstring only)
├── auth.py            # initData / contact-response validation, user resolution, @authed decorator
├── errors.py          # ServiceError -> HTTP status + JSON body; ApiError for web-only errors
├── serializers.py     # DTO -> JSON dicts (pure, sync, no DB)
├── photos.py          # barber photo proxy with an in-memory cache (Telegram file_id -> bytes)
├── locks.py           # booking_lock / user_lock, Idempotency-Key TTL map [J-A1, J-D1]
├── tasks.py           # tracked notification tasks + drain on stop [J-D2]
├── ratelimit.py       # sliding-window limiter (per tg_id, per IP) [J-A6]
├── api.py             # all /api/* route handlers (aiohttp RouteTableDef)
├── server.py          # create_app(...) factory, start_webapp(...) / WebAppHandle.stop()
└── static/
    ├── index.html
    ├── style.css
    ├── app.js         # entry module: boot, router, Telegram SDK glue
    ├── api.js         # fetch wrapper (auth header, JSON, errors)
    ├── tg.js          # Telegram.WebApp helpers: MainButton, BackButton, haptics, theme
    ├── ui.js          # small DOM helpers: h(), toast, sheet, skeleton, fmtPrice, fmtDate
    └── screens/
        ├── home.js  book.js  bookings.js  booking.js  bonus.js
        ├── barbers.js  barber.js  consent.js  phone.js
```

### 2.1 `server.py`

```python
from aiohttp import web

# typed app keys (aiohttp >= 3.9 AppKey)
SM_KEY       = web.AppKey("sm", async_sessionmaker)
BOT_KEY      = web.AppKey("bot", Bot)
NOTIFIER_KEY = web.AppKey("notifier", Notifier)
BOT_USERNAME_KEY = web.AppKey("bot_username", str)
TOKEN_KEY    = web.AppKey("bot_token", str)
PHOTOS_KEY   = web.AppKey("photos", PhotoCache)
LIMITER_KEY  = web.AppKey("limiter", RateLimiter)

def create_app(*, sm, bot, notifier, bot_token: str, bot_username: str,
               auth_max_age: int | None = None) -> web.Application: ...
    # middlewares: [error_middleware, security_headers_middleware]
    # routes: api.routes (prefix /api), GET /healthz, GET / -> static/index.html,
    #         static files under /static/ (web.static, show_index=False, follow_symlinks=False)
    # client_max_size = 64 KiB

@dataclass
class WebAppHandle:
    runner: web.AppRunner
    async def stop(self) -> None:
        await self.runner.cleanup()
        await drain_pending_notifications(timeout=10)   # gather(*_PENDING, return_exceptions=True) [J-D2]

async def start_webapp(app: web.Application, *, host: str, port: int) -> WebAppHandle:
    runner = web.AppRunner(app, access_log=None)   # no access log: initData is in headers, not in URLs
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return WebAppHandle(runner)
```

- **Static files:** `index.html` is served at `/` with `Cache-Control: no-cache`. Files under `/static/` get `Cache-Control: no-cache` too. Revalidation by Last-Modified is cheap, and it avoids stale JS after a deploy without a build step.
- **Security headers on every response:**
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `Content-Security-Policy: default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org 'self'; base-uri 'none'; form-action 'none'; object-src 'none'` [J-B8] (`'unsafe-inline'` styles are needed because Telegram injects theme variables). Do **not** send `X-Frame-Options`, because Telegram Web runs Mini Apps in an iframe.
- `GET /healthz` returns `{"ok": true}` and needs no auth. No version or other details.
- **No CORS headers, ever.** No `Access-Control-*` header on any response, and OPTIONS gets no `Allow-Origin` [J-B6].
- Log one INFO line at start: `webapp listening on http://HOST:PORT (public URL: … | buttons hidden)`.

### 2.2 Start and stop with the bot (`app/bot/main.py`)

In `run()`, after `build_dispatcher(...)`, `set_default_commands` and `scheduler.start(...)`, and before `dp.start_polling`:

```python
webapp = None
if cfg.webapp_enabled:
    from app.webapp.server import create_app, start_webapp
    app = create_app(sm=sm, bot=bot, notifier=notifier,
                     bot_token=cfg.bot_token.get_secret_value(), bot_username=me.username or "")
    try:
        webapp = await start_webapp(app, host=cfg.webapp_host, port=cfg.webapp_port)
    except OSError as e:          # port in use etc.: the bot keeps working without the web app
        log.error("webapp could not start on %s:%s: %s", cfg.webapp_host, cfg.webapp_port, e)
await setup_menu_button(bot, cfg.webapp_public_url if webapp else None)   # app/bot/menu_button.py
try:
    await dp.start_polling(...)
finally:
    if webapp: await webapp.stop()      # before scheduler.shutdown / storage.close / bot.session.close
    scheduler.shutdown(); ...
```

- One process and one event loop. `bot.lock` still guarantees a single instance, so the web server never runs twice.
- `handle_signals=True` stays. SIGTERM stops polling, then the `finally` block stops aiohttp gracefully.
- The URL in `views.hub_content` needs the config only. Read `get_config().webapp_public_url` and check a module-level flag `app.webapp.server.RUNNING: bool`, which `start_webapp` sets and `stop()` clears. This hides the button if the server failed to start.

### 2.3 Menu button (`app/bot/menu_button.py`, new, BACKEND)

```python
async def setup_menu_button(bot: Bot, url: str | None) -> None:
    """Default chat menu button: the Mini App when url is set, otherwise the command list."""
    try:
        if url:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=url)))
        else:
            cur = await bot.get_chat_menu_button()
            if isinstance(cur, MenuButtonWebApp) and cur.text == "Ilova":   # only undo OUR previous run [J-E]
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as e:
        log.warning("set_chat_menu_button failed: %s", e)
```

It runs only without `chat_id`, which sets the default for every private chat. A failure is cosmetic and never blocks startup.

### 2.4 "🌐 Ilovani ochish" button in the client main menu

**Telegram limitation:** a Mini App opened from a **reply-keyboard** `KeyboardButton(web_app=…)` gets no `initData`. It can only `sendData`, so our auth cannot work from it. The button therefore goes on the client **hub message** (the client's main menu screen, `views.hub_content`) as an **inline** `InlineKeyboardButton(text=t(lang,"hub.webapp"), web_app=WebAppInfo(url=url))`. It is the first row, above "✂️ Navbatga yozilish", and appears only when the URL is set and the server is running. The reply keyboard stays unchanged. Together with the chat menu button this covers "open from the main menu".

New locale keys, the same key in both files:
- `uz.py`: `"hub.webapp": "🌐 Ilovani ochish"`
- `ru.py`: `"hub.webapp": "🌐 Открыть приложение"`
- `uz.py`: `"contact.saved_webapp": "✅ Raqamingiz saqlandi. Ilovaga qayting."`
- `ru.py`: `"contact.saved_webapp": "✅ Номер сохранён. Вернитесь в приложение."`

### 2.5 Contact received in chat (`app/bot/handlers/webapp_contact.py`, new)

When the Mini App calls `requestContact`, Telegram also posts the user's contact into the bot chat as a message. Today a contact message outside `BookingFlow.contact` and `ManualBookingFlow.phone` falls into `fallback`. Add a router:

```python
@router.message(F.contact)      # included in build_root_router() AFTER client_booking and barber_manual, BEFORE fallback
async def on_any_contact(message, sm, user, lang):
    if message.contact.user_id != message.from_user.id: -> answer t(lang,"contact.only_own"); return
    write_tx: users.set_verified_contact(s, user_id=user.id, raw_phone=message.contact.phone_number)
    ServiceError -> answer views.err_text(lang, e)
    ok -> answer t(lang, "contact.saved_webapp"), unless the user already had this same verified phone
          before the call (the Mini App saved it first): then stay silent [J-C3]
```

This is the fallback path. The primary path is §4.3 `POST /api/me/contact`. Both call the same service and are idempotent.

---

## 3. Auth (`app/webapp/auth.py`)

### 3.1 Transport

Every `/api/*` request except `/api/barbers/{id}/photo` carries this header:

```
Authorization: tma <Telegram.WebApp.initData>
```

`initData` is never sent in URLs, so it never appears in proxy access logs.

### 3.2 Validation (Telegram spec)

```python
def validate_init_data(init_data: str, bot_token: str, *, max_age: int, now: float | None = None) -> WebAppInitData:
```

1. Reject a header value longer than 4096 bytes. Parse with `urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)`. Reject duplicate keys. Any failure is `bad_init_data` [J-B2].
2. Remove `hash`, which is required. Build `data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs))`. The `signature` field, if present, stays in the check string, exactly as Telegram's algorithm requires.
3. Compute the signature:
   - `secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`
   - `calc = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()`
   - Compare with `hmac.compare_digest(calc, hash.lower())`.
   - `aiogram.utils.web_app.check_webapp_signature` implements exactly this and may be used. In that case keep a thin wrapper so the max-age and parsing rules stay ours.
4. `auth_date` must be an int with `now - auth_date <= max_age`, where `max_age` is `webapp_auth_max_age` (86400) for GET and `webapp_auth_max_age_write` (86400 since the judge pass; was 3600) for POST [J-B1]. It must also not lie in the future by more than 60 s, to allow for clock skew.
5. `user` (JSON) is required and must have an int `id` and a `first_name`. Without it the request fails with 401 `no_user`. That happens when the app is opened from a reply-keyboard button or outside Telegram.
6. Return the parsed fields we need: `user.id`, `user.first_name`, `user.last_name`, `user.username`, `user.language_code`, `auth_date` and `start_param`. Use `aiogram.utils.web_app.safe_parse_webapp_init_data` or our own dataclass.

Failure means HTTP 401 with this body:

```json
{"error": {"code": "unauthorized", "message": "…", "reason": "bad_hash|expired|bad_init_data|no_user|missing"}}
```

Never log `initData`, the hash or `request.headers`. The error middleware logs only method, path and tg_id [J-B1].

### 3.3 User resolution (mirrors `UserMiddleware` exactly)

```python
async def resolve_user(sm, init: WebAppInitData) -> UserContext:
    read_tx:  ctx = users.load_context(s, tg_id=user.id)
    if ctx is None or users.needs_sync(ctx.user, tg_username=..., first_name=...):   # NOT start_param [J-A5]
        write_tx: ctx = users.ensure_user(s, tg_id=..., tg_username=..., first_name=...,
                                          language_code=..., start_payload=start_param)
    if ctx.roles.is_barber and ctx.barber_reachable is False:
        write_tx: barbers.mark_reachable_by_user(s, user_id=ctx.user.id)    # same as middleware [B4]
    return ctx
```

- `start_param` comes from `t.me/<bot>/<app>?startapp=ref_xxx` or `src_xxx`. It is treated exactly like a `/start` payload, so referrals work from the web too. `ensure_user` only applies it to new users, so it is passed only when a write happens anyway. Unknown payloads such as `barber` are ignored by `ensure_user` (it only acts on `ref_`/`src_`).
- Roles are re-read on every request and never cached [H2].

### 3.4 `@authed` decorator and gates

```python
def authed(*, consent: bool = True, barber: bool = False): ...
# handler signature: async def h(request: web.Request, ctx: UserContext) -> web.Response
```

Order of checks [J-A6]:
1. Missing or invalid header → 401 `unauthorized`. 401s are also counted per IP (60/min, then 429).
2. Rate limit on the validated `tg_id`, **before any DB access**. Reads (GET) get 120 per minute. Writes (POST) get `cfg.throttle_per_minute` per minute, default 30, the same budget as the bot. The limiter is an in-memory sliding window that keeps at most 10 000 keys and clears itself when full. Exceeding it returns 429 `throttled`.
3. Resolve the user (§3.3).
4. `ctx.user.is_blocked` → 403 `user_blocked`. This applies to every endpoint, `/api/me` included. The frontend shows a blocked screen.
5. If `consent=True` and `(ctx.user.consent_version or 0) < CONSENT_VERSION` → 403 `consent_required`. Only `GET /api/me` and `POST /api/me/consent` use `consent=False`.
6. If `barber=True` and not `(ctx.roles.is_barber and ctx.roles.barber_id)` → 403 `forbidden`. Services check permissions again on top of this.
7. POST requests must have `Content-Type: application/json`, otherwise 400 `invalid`, `field: "body"` [J-B6].

---

## 4. REST API (`app/webapp/api.py`)

### 4.0 Conventions

- JSON in and out, UTF-8. The request body must be a JSON object. If it is not, the request fails with 400 `invalid` and `field: "body"`.
- **Times:** every instant is ISO-8601 UTC with `Z` (`"2026-10-25T05:30:00Z"`). Every instant shown to people also comes with a `local` object computed on the server in `SHOP_TZ`. The frontend **never** converts time zones.

  ```json
  "local": {"date": "2026-10-26", "time": "10:30", "end_time": "11:00", "weekday": 0, "label": "26-oktabr, dushanba"}
  ```

  `weekday` runs 0 (Monday) to 6, as in Python. `label` is exactly `app.bot.i18n.fmt_date(day, "uz")`, so wording and order match the bot. The frontend never builds labels itself [J-A8].
- **Parsing instants** (`start_at` in bodies): `datetime.fromisoformat(s.replace("Z", "+00:00"))` (Python 3.10 compatible), require `tzinfo`, convert to UTC, and reject non-zero seconds or microseconds [J-A4].
- **"Today"** is always `clock.local_today()` (SHOP_TZ), never `date.today()` [J-D4].
- **Days** are `"YYYY-MM-DD"` local dates.
- **Money** is an integer in UZS. The frontend formats it as `60 000 so'm`.
- Every write handler follows the rule below. There is never `await notifier…` inside `write_tx`.

  ```python
  try:
      async with write_tx(sm) as s:
          result = await service(...)
  except ServiceError as e:
      return error_response(e)
  await notifier.<event>(...)        # AFTER commit, outside the tx, same call as the bot handler
  return json(...)
  ```

  Notifier methods are `@_guard`ed, so a Telegram failure never fails the HTTP response.
  - `booking_created` is **awaited inline**, so decision message ids exist before anyone can accept in the app [J-D2].
  - Other notifications may be scheduled with `asyncio.create_task(...)`. Each task is kept in a module-level `_PENDING` set until done, and `WebAppHandle.stop()` drains it (10 s timeout) before `bot.session.close()`.
- **Booking state changes** (cancel, proposal, accept, reject, done, no-show) follow this stricter rule [J-A1, J-A2]:

  ```python
  async with booking_lock(booking_id):            # per-booking asyncio.Lock, in-process, weak dict
      try:
          async with write_tx(sm) as s:           # BEGIN IMMEDIATE on SQLite, so the snapshot is serialised
              before = await bookings.get_booking_for_actor(s, booking_id=id, actor_user_id=uid)
              result = await service(...)          # idempotent: an already-applied transition is a no-op
      except ServiceError as e:
          return error_response(e)
  if changed(before, result):                      # e.g. before.status != result.booking.status
      await/schedule notifier.<event>(...)
  return json(...)
  ```

  A repeated tap, two open copies of the app, or a decision already made in Telegram returns **200 with the current booking** and sends **no** second notification.
- **Language:** the UI is Uzbek. Error `message` strings use `views.err_text("uz", e)`, so the texts are the bot's own `err.*` strings.

### 4.1 Error format (`app/webapp/errors.py`)

```json
{"error": {"code": "slot_taken", "message": "😔 Bu vaqtni hozirgina boshqa mijoz band qildi. Boshqa vaqtni tanlang.", "ctx": {"kind": "overlap", "limit": 3}}}
```

`ctx` contains only JSON scalars. Datetimes become ISO strings, and anything else is dropped.

| code | HTTP |
|---|---|
| `unauthorized` | 401 |
| `user_blocked`, `consent_required`, `forbidden` | 403 |
| `not_found` | 404 |
| `invalid`, `invalid_phone`, `invalid_name` | 400 |
| `phone_required` | 422 |
| `slot_taken`, `slot_unavailable`, `barber_inactive`, `client_limit`, `invalid_transition`, `too_early`, `too_late`, `no_bonus`, `proposal_limit` | 409 |
| `throttled` | 429 |
| any other `ServiceError` | 400 |
| unexpected exception | 500, code `error`; logged with traceback, no details in the body |

`error_middleware` catches `ServiceError`, `ApiError(code, status, message, **ctx)` and `Exception`. 404 and 405 from aiohttp become the same JSON shape under `/api`.

### 4.2 Serializers (`app/webapp/serializers.py`)

All functions are pure and take an optional `now`.

**`user_json(ctx, bonus=None)`**

```json
{
  "id": 12, "first_name": "Ali", "display_name": "Ali Valiyev",
  "phone": "+998901234567", "phone_verified": true, "name_ok": true,
  "lang": "uz",
  "roles": {"client": true, "barber": false, "admin": false, "barber_id": null},
  "consent_required": false, "blocked": false
}
```

`name_ok` is true when `validation.validate_name(display_name)` succeeds.

**`booking_json(b, *, viewer: "client"|"barber", shop: ShopSettings, now)`**

```json
{
  "id": 41, "status": "CONFIRMED", "awaiting": null, "source": "bot",
  "status_label": "Tasdiqlangan",
  "barber": {"id": 2, "name": "Jasur"},
  "service": {"id": 1, "name": "Soch olish"},
  "start_at": "…Z", "end_at": "…Z", "local": {…}, "duration_min": 30,
  "price_uzs": 60000, "final_price_uzs": 60000, "bonus_redeemed": false, "bonus_cost": 0,
  "comment": null, "expire_at": null, "original_start_at": null, "original_local": null,
  "proposal_count": 0, "late_cancel": false, "cancel_reason": null, "cancel_reason_label": null,
  "client": {"name": "Ali", "phone": "+998 90 *** ** 67", "stats": {"visits": 4, "no_shows": 0, "is_new": false}},
  "actions": {"cancel": true, "withdraw": false, "late_cancel_warning": false, "answer_proposal": false,
              "accept": false, "reject": false, "done": false, "no_show": false}
}
```

- `client` is included only when `viewer == "barber"`. `stats` comes from `users.get_client_stats` and is filled in by the API handler, not the serializer.
- `status_label` uses `t("uz", "status.<STATUS>")`. For PENDING with `awaiting == "client"` it uses `status.PENDING_CLIENT`.
- `cancel_reason_label` uses `reason.<x>` when that key exists, otherwise the raw text.
- `actions` for the **client** view. These are the same predicates as `client_my`. The service still decides.
  - `cancel`: status in (PENDING, CONFIRMED) and `start_at > now`, except PENDING awaiting barber, which is a `withdraw`.
  - `withdraw`: PENDING and `awaiting == barber`.
  - `late_cancel_warning`: CONFIRMED and `start_at - now < free_cancel_hours`.
  - `answer_proposal`: PENDING and `awaiting == client`.
- `actions` for the **barber** view [J-D5]:
  - `accept`: PENDING and `awaiting == barber`, **or** EXPIRED with `awaiting == barber`, `now <= expire_at + late_accept_grace_min` and `start_at > now` (same as the bot).
  - `reject`: PENDING and `awaiting == barber`.
  - `done` and `no_show`: CONFIRMED. Also send `done_from` (`start_at - 30 min`) and `no_show_from` (`start_at + 15 min`) as ISO instants; the frontend shows the buttons disabled until then.
- `client.phone` may be masked by the service (`*` in it). The frontend renders a `tel:` link only when the phone contains no `*` [J-D6].
- `source` stays `"bot"` for web bookings. **Do not add `BookingSource.WEBAPP`**: `Notifier._client_targetable` requires `source == BOT`, so a new value would silently stop client notifications [J-A3].

**Other serializers:**
- `day_json(DayAvailability)` → `{"day", "label", "short": "Du 25", "is_working", "has_free_slots", "is_today", "is_tomorrow"}`
- `slot_json(dt)` → `{"start_at", "time": "10:30", "group": "morning|day|evening"}`. The group is morning before 12:00, day before 17:00 and evening otherwise, the same rule as `keyboards/client.py`.
- `bonus_json(BonusStatus)` → every BonusStatus field, plus `recent[]` items `{id, delta, unit, reason, reason_label, comment, created_at, local}`. `reason_label` uses `t("uz", "bonus.reason.<r>")`.
- `shop_json(ShopSettings, week: list[ShopDayHours])` → `{"name", "address", "phone", "latitude", "longitude", "map_yandex_url", "map_google_url", "map_url", "hours": [{"weekday", "open": "10:00"|null, "close": "20:00"|null}], "free_cancel_hours", "booking_horizon_days"}`. `map_url` is `map_google_url`, else `map_yandex_url`, else `https://maps.google.com/?q=<lat>,<lon>` when coordinates exist, else null.

### 4.3 Me

#### `GET /api/me`

Uses `consent=False`.

Response:

```json
{
  "user": {…user_json…},
  "consent_text": "<plain text>",
  "bot_username": "sartarosh_bot",
  "webapp": {"auth_max_age": 86400}
}
```

`consent_text` is present only when `consent_required` is true. It is `t("uz", "onb.consent")` with HTML tags stripped.

Services: `resolve_user` only.

#### `POST /api/me/consent`

Uses `consent=False`. The body is `{}`.

Calls `users.accept_consent(s, user_id, version=CONSENT_VERSION)` in a write tx.

Response: `{"user": {…}}`.

#### `POST /api/me/contact`

Body: `{"response": "<string from requestContact callback .response>"}`.

1. Validate `response` with the **same** HMAC algorithm as initData (§3.2, same secret, `max_age` = 600 s). Failure → 400 `invalid`, `field: "contact"`.
2. `contact = json.loads(params["contact"])`. Require `contact["user_id"] == ctx.user.tg_id`. Otherwise → 403 `forbidden`, matching the bot's `contact.only_own` rule. Require a non-empty `contact["phone_number"]`, otherwise 400 `invalid`, `field: "contact"`.
3. In a write tx, call `users.set_verified_contact(s, user_id=ctx.user.id, raw_phone=contact["phone_number"])`.

Errors: `invalid_phone` (ctx `reason: "foreign"`).

Response: `{"user": {…}}`.

#### `POST /api/me/name`

Body: `{"name": "Ali Valiyev"}`.

Calls `users.set_full_name`. Errors: `invalid_name`.

Response: `{"user": {…}}`.

### 4.4 Home and catalog (client)

#### `GET /api/home`

All reads happen in one `read_tx`.

| field | source |
|---|---|
| `shop` | `settings.get_shop_settings`, `schedule.get_shop_week_hours` → `shop_json` |
| `user` | `user_json` |
| `bonus` | `bonus.get_bonus_status(s, uid, recent_limit=0)` → `{enabled, mode, balance, available, required, progress, can_redeem}` |
| `upcoming` | `bookings.list_client_bookings(s, client_user_id=uid, past_limit=0)`: `.upcoming[0]` or null, plus `upcoming_count` |
| `rebook` | `bookings.get_last_completed(s, client_user_id=uid)` → `{barber_id, barber_name, service_id, service_name}` or null |
| `referral_enabled` | `shop.referral_enabled and shop.loyalty_enabled` |
| `is_barber` | `roles.is_barber and roles.barber_id`, which shows the "Usta paneli" entry |

#### `GET /api/barbers`

The barbers page.

Services:
- `barbers.list_barbers(s, public_only=True)`
- for each barber, `catalog.list_barber_services(s, barber_id)`

Response:

```json
{"barbers": [{"id", "name", "bio", "photo_url": "/api/barbers/2/photo?v=<8 hex of sha1(file_id)>" | null,
              "services": [{"service_id", "name", "duration_min", "price_uzs"}]}]}
```

#### `GET /api/barbers/{id}/photo`

**No auth.** An `<img>` cannot send headers, and barber photos are public in the bot anyway.

1. In a `read_tx`, call `barbers.get_barber`. Return 404 unless the barber is active, public and has `photo_file_id`.
2. `photos.PhotoCache.get(file_id)`:
   - Call `bot.get_file`, then `bot.download(file, destination=BytesIO)`.
   - Keep at most 30 entries of at most 1 MB each (larger files are served but not cached), with a TTL of 24 h.
   - Failures are cached negatively for 10 min (404 without calling Telegram) [J-B5].
   - One `asyncio.Lock` per file id, so concurrent requests download once.
   - Per-IP limit 60/min (`X-Forwarded-For` only when `WEBAPP_TRUST_PROXY=true`, else the peer address). The `?v=` query is a cache-buster and is ignored by the server.
3. Response: `image/jpeg`, `Cache-Control: public, max-age=86400`.

Errors from Telegram → 404.

### 4.5 Booking wizard (client)

Each endpoint below mirrors the step of the same name in `client_booking.py`.

#### `GET /api/booking/services`

Services:
- `catalog.shop_has_services`
- `catalog.list_bookable_services`

Response:

```json
{"has_services": true, "services": [{"id", "name", "duration_min", "price_uzs"}]}
```

`price_uzs` is the base price, and the frontend shows it as "…dan" when barbers' prices differ. If `has_services` is false, the frontend skips the service step and sends `service_id: null` from then on.

#### `GET /api/booking/barbers?service_id=<int|empty>`

Services:
- `catalog.list_barbers_for_service(s, service_id=…)`
- for each barber, `catalog.resolve_service(s, barber_id, service_id)`. Skip barbers that raise `InputError`, exactly like `_price_info`.

Response:

```json
{
  "any": {"available": true, "price_uzs": 50000, "price_varies": true, "duration_min": 30},
  "barbers": [{"id", "name", "photo_url", "price_uzs", "duration_min"}]
}
```

The `any` row is the lowest `(price, duration)` over the options. It is only `available` when there are at least 2 barbers. With exactly one barber the frontend preselects that barber and skips this step.

#### `GET /api/booking/days?barber_id=<int|empty>&service_id=<int|empty>`

Service: `slots.get_available_days(s, barber_id=… or None, service_id=… or None)`.

Response: `{"days": [day_json…]}`, from today up to today + horizon − 1.

#### `GET /api/booking/slots?barber_id=&service_id=&day=YYYY-MM-DD`

Services:
- with a barber: `slots.get_free_slots(s, barber_id=…, service_id=…, day=…)`
- with `barber_id` empty: `slots.get_free_slots_any(s, service_id=…, day=…)`

Response: `{"day": day, "slots": [slot_json…]}`.

A bad `day`, or one outside today … today + horizon − 1, → 400 `invalid`, `field: "day"` [J-D4].

#### `POST /api/booking/quote`

Body: `{"barber_id": int|null, "service_id": int|null, "start_at": "…Z"}`.

This is the read-only summary screen. All reads happen in one `read_tx`, the same logic as `_draw_confirm` and `_price_info`.
1. Resolve the price and duration (the barber's, or the lowest for "any", with `price_varies`).
2. `bonus.quote_redeem(s, user_id, price_uzs=price)` when `price > 0`.
3. `users.is_soft_blocked`.
4. `users.get_user`.
5. `barbers.get_barber` for the name.
6. Check that `start_at` is still in the slot list for that barber, or for "any". If not, the response carries `slot_free: false`.

Response:

```json
{
  "barber": {"id": 2, "name": "Jasur"} | null,
  "service": {"id": 1, "name": "Soch olish", "duration_min": 30} | null,
  "start_at": "…Z", "local": {…},
  "price_uzs": 60000, "price_varies": false,
  "bonus": {"possible": true, "cost": 5, "final_price_uzs": 0, "discount_uzs": 60000,
            "mode": "stamps", "available": 5} | null,
  "soft_blocked": false, "needs_approval_hint": true,
  "slot_free": true,
  "phone_ok": true, "name_ok": true,
  "user": {"display_name": "Ali", "phone": "+998…"}
}
```

`needs_approval_hint` is `soft_blocked or barber.auto_accept is false`. It is only a hint. The result of `create` decides.

With "any barber" the price and the bonus discount are lowest-case estimates ("…dan"). The success screen shows `final_price_uzs` from the 201 response, never the quote [J-D3].

#### `POST /api/bookings`

Body:

```json
{"barber_id": int|null, "service_id": int|null, "start_at": "…Z", "comment": "str|null", "use_bonus": false}
```

0. Header `Idempotency-Key: <uuid>` is required (the frontend generates it when the confirm screen opens). The whole handler runs under a per-user `asyncio.Lock`. An in-memory map `(tg_id, key) → (status, body)` with a 10 min TTL (max 10 000 entries) returns the stored 201 on a repeat, without calling the service or the notifier [J-D1].
1. Parse `start_at` per §4.0. A bad value → 400 `invalid`, `field: "start_at"`.
2. `comment = validation.validate_comment(comment)`. Errors give 400 with `field: "comment"`.
3. In a write tx:

   ```python
   res = await bookings.create_booking(s, client_user_id=ctx.user.id, barber_id=…, service_id=…,
                                       start_at=…, comment=…, use_bonus=bool(use_bonus))
   ```

4. After commit, `await notifier.booking_created(res)` inline (not a task) [J-D2]. This is identical to the bot. A PENDING booking sends the barber and the admins the decision message with `[✅ Qabul qilish] [❌ Rad etish] [🕒 Boshqa vaqt taklif qilish]` and stores decision message ids. An auto-confirmed booking sends the barber the "new confirmed" card and the client the venue.

Response 201:

```json
{"booking": {…booking_json client…}, "auto_confirmed": true, "soft_blocked": false}
```

Errors, and what the frontend does with each:

| error | frontend action |
|---|---|
| 409 `slot_taken` / `slot_unavailable` | back to the slot grid, refetch, show the message |
| 422 `phone_required` | phone screen |
| 400 `invalid_name` | name input |
| 409 `no_bonus` | turn the bonus toggle off, requote |
| 409 `client_limit` (ctx `kind`, `limit`) | show the message |
| 409 `barber_inactive` | back to the barber step |
| 403 `consent_required` | consent screen |

### 4.6 My bookings (client)

#### `GET /api/bookings`

Service: `bookings.list_client_bookings(s, client_user_id=uid, past_limit=20)`.

Response: `{"upcoming": [booking_json…], "past": [booking_json…]}`.

#### `GET /api/bookings/{id}`

Service: `bookings.get_booking_for_actor(s, booking_id=id, actor_user_id=uid)`.

The response uses the client view if `b.client_id == uid`. Otherwise it uses the barber view, and only if `roles.barber_id == b.barber_id`. Otherwise it returns 403. Admins also use the barber view.

Errors: 404, 403.

#### `POST /api/bookings/{id}/cancel`

Body: `{"withdraw": false}`.

Runs under `booking_lock(id)` (§4.0). Steps 1 to 4 happen in **one** write tx [J-A2].
1. `get_booking_for_actor`. Return 403 unless `b.client_id == uid`.
2. Pre-checks, the same as `client_my`. If the booking is already CANCELLED_BY_CLIENT, skip the pre-checks and return 200 with the booking and no notification:
   - `withdraw` requires PENDING and `awaiting == barber`.
   - otherwise status must be in (PENDING, CONFIRMED) and `start_at > now`.
   - A failed pre-check → 409 `invalid_transition` with message `t("uz", "my.cannot_cancel")`.
3. `was_active = status in ACTIVE_STATUSES`.
4. `res = bookings.cancel_by_client(s, booking_id=id, client_user_id=uid, reason="withdrawn" if withdraw else None)`.
5. After commit, if `was_active` and the status changed, call `notifier.booking_cancelled(res.booking, by_client=True)`. That edits the barber and admin decision messages and tells the barber, including the late wording.

Response: `{"booking": {…}, "late": res.late}`.

The late-cancel policy (late flag, `late_cancel_count`, no-show conversion) lives entirely in `cancel_by_client`. The web app only shows the warning beforehand, using `actions.late_cancel_warning` and `shop.free_cancel_hours`.

#### `POST /api/bookings/{id}/proposal`

Body: `{"accept": true}`.

1. Under `booking_lock(id)`, in one write tx: `was_open` = PENDING and `awaiting == client` (from `get_booking_for_actor`), then `bookings.answer_proposal(s, booking_id=id, client_user_id=uid, accept=accept)` [J-A2].
2. After commit, if `was_open`, call `notifier.proposal_answered(nb)`.

Response: `{"booking": {…}}`.

### 4.7 Bonus (client)

#### `GET /api/bonus`

Services:
- `bonus.get_bonus_status(s, uid, recent_limit=50)`
- `settings.get_shop_settings` for the referral part

Response:

```json
{
  "bonus": {…bonus_json…},
  "referral": {"enabled": true, "bonus": 1, "link": "https://t.me/<bot_username>?start=ref_<user.ref_code>"}
}
```

`referral.enabled` is `shop.referral_enabled and shop.loyalty_enabled`. The link uses the same format as `client_info.show_referral`.

### 4.8 Barber panel

Every endpoint here uses `@authed(barber=True)`, and `barber_id = ctx.roles.barber_id`.

#### `GET /api/barber/days`

Uses the same rule as `barber_queue.show_day_picker`: `n = max(7, shop.booking_horizon_days)` days from today, capped at 21.

Response:

```json
{"barber": {"id", "name"}, "today": "YYYY-MM-DD", "days": [{"day", "label", "short", "is_today"}]}
```

#### `GET /api/barber/queue?day=YYYY-MM-DD`

`day` defaults to `clock.local_today()`. It must lie in today … today + 20, otherwise 400 `invalid`, `field: "day"` [J-D4].

Services:
- `bookings.list_barber_day(s, actor_user_id=uid, barber_id=barber_id, day=day)`, sorted by `start_at`
- for each booking with `client_id`, `users.get_client_stats(s, client_id)`

All of this happens in one `read_tx`. Phone masking for old bookings is already applied by the service.

Response:

```json
{"today": "YYYY-MM-DD", "day": day_json, "bookings": [booking_json barber view…], "pending_count": 2}
```

#### `POST /api/barber/bookings/{id}/accept`

1. Under `booking_lock(id)`, in a write tx: snapshot `before`, then `b = bookings.confirm_booking(s, booking_id=id, actor_user_id=uid)`. The service also handles EXPIRED within `late_accept_grace`.
2. Only if `before.status != CONFIRMED`, call `notifier.booking_confirmed(b)` with no `skip`. That edits every decision message (barber and admins) to the outcome text and notifies the client.

Response: `{"booking": {…}}`.

Errors: 409 `invalid_transition` (decided the other way, e.g. already rejected), `too_late`, `slot_taken` (late accept), 403. Accepting an already CONFIRMED booking is 200 with no notification [J-A1].

#### `POST /api/barber/bookings/{id}/reject`

Body: `{"reason": "busy" | "sick" | null | "<free text>"}`.

1. Under `booking_lock(id)`, in one write tx with step 3: snapshot `before`. If it is already REJECTED, return 200 and do not notify. Otherwise it must be PENDING and `awaiting == barber`, else 409 `invalid_transition`.
2. Free text goes through `validation.validate_comment(...)[:200]` and must not be empty after that. `null` means "no reason", as the bot's `none` button does.
3. Call `bookings.reject_booking(s, booking_id=id, actor_user_id=uid, reason=reason)`.
4. After commit, if `before.status != REJECTED`, call `notifier.booking_rejected(b)`.

Response: `{"booking": {…}}`.

#### `POST /api/barber/bookings/{id}/done`

1. Under `booking_lock(id)`, in a write tx: snapshot `before`, then `r = bookings.complete_booking(s, booking_id=id, actor_user_id=uid)`.
2. Only if `before.status != COMPLETED`, call `notifier.booking_completed(r)`. Otherwise return the booking with `notes: []`.

Response:

```json
{"booking": {…}, "notes": ["…"]}
```

`notes` uses the same keys as `barber_queue`: `barber.done_ok`, then `barber.bonus_failed` or `barber.bonus_applied`, then `barber.visit_pending`. Each is rendered with `t("uz", …)` and stripped of HTML.

#### `POST /api/barber/bookings/{id}/no-show`

1. Under `booking_lock(id)`, in a write tx: snapshot `before`, then `r = bookings.mark_no_show(...)`.
2. Only if `before.status != NO_SHOW`, call `notifier.booking_no_show(r)`.

Response: `{"booking": {…}, "notes": [no_show_ok, reversed?]}`.

Errors for `done` and `no-show`: `too_early`, `too_late`, `invalid_transition`.

**Out of scope for v1 in the web app:** propose another time, staff cancel, block time, manual booking and admin screens. These stay in the bot. The barber screen shows a "Botda ochish" link (`https://t.me/<bot_username>`) for them.

### 4.9 Endpoint summary

| Method | Path | Auth | Service call(s) | Notifier after commit |
|---|---|---|---|---|
| GET | /healthz | – | – | – |
| GET | /api/me | tma, no consent | users.load_context / ensure_user | – |
| POST | /api/me/consent | tma, no consent | users.accept_consent | – |
| POST | /api/me/contact | tma | users.set_verified_contact | – |
| POST | /api/me/name | tma | users.set_full_name | – |
| GET | /api/home | tma | settings.get_shop_settings, schedule.get_shop_week_hours, bonus.get_bonus_status, bookings.list_client_bookings, bookings.get_last_completed | – |
| GET | /api/barbers | tma | barbers.list_barbers, catalog.list_barber_services | – |
| GET | /api/barbers/{id}/photo | none | barbers.get_barber + Bot.get_file/download | – |
| GET | /api/booking/services | tma | catalog.shop_has_services, catalog.list_bookable_services | – |
| GET | /api/booking/barbers | tma | catalog.list_barbers_for_service, catalog.resolve_service | – |
| GET | /api/booking/days | tma | slots.get_available_days | – |
| GET | /api/booking/slots | tma | slots.get_free_slots / get_free_slots_any | – |
| POST | /api/booking/quote | tma | catalog.resolve_service, bonus.quote_redeem, users.is_soft_blocked, users.get_user, slots.* | – |
| POST | /api/bookings | tma | bookings.create_booking | booking_created(res) |
| GET | /api/bookings | tma | bookings.list_client_bookings | – |
| GET | /api/bookings/{id} | tma | bookings.get_booking_for_actor | – |
| POST | /api/bookings/{id}/cancel | tma | bookings.cancel_by_client | booking_cancelled(b, by_client=True) |
| POST | /api/bookings/{id}/proposal | tma | bookings.answer_proposal | proposal_answered(b) |
| GET | /api/bonus | tma | bonus.get_bonus_status, settings.get_shop_settings | – |
| GET | /api/barber/days | tma+barber | settings.get_shop_settings, barbers.get_barber | – |
| GET | /api/barber/queue | tma+barber | bookings.list_barber_day, users.get_client_stats | – |
| POST | /api/barber/bookings/{id}/accept | tma+barber | bookings.confirm_booking | booking_confirmed(b) |
| POST | /api/barber/bookings/{id}/reject | tma+barber | bookings.reject_booking | booking_rejected(b) |
| POST | /api/barber/bookings/{id}/done | tma+barber | bookings.complete_booking | booking_completed(r) |
| POST | /api/barber/bookings/{id}/no-show | tma+barber | bookings.mark_no_show | booking_no_show(r) |

---

## 5. Notifications (reuse, not duplicate)

- The `Notifier` instance created in `build_dispatcher` is passed into `create_app`. There is exactly one notifier per process, so pacing (≤20 concurrent, ≤25 msg/s), RetryAfter handling and "barber unreachable" logic are shared with the bot.
- The web API calls the **same method with the same arguments** as the matching bot handler (table in §4.9). It never sends Telegram messages itself, except for the photo download in §4.4.
- A booking created from the web therefore produces, for a PENDING booking, the decision message with Accept/Reject/Propose buttons in the barber's chat and every admin's chat. When the barber then taps "✅ Qabul qilish" in Telegram, the existing `barber_decide` handler runs. When they accept in the web app instead, `notifier.booking_confirmed` edits those Telegram messages to the outcome. Both paths stay consistent because the decision message ids are in the DB (`add_decision_message`).
- The scheduler (reminders, expiry, re-pings) needs no change. It works from DB state.

---

## 6. Frontend (`app/webapp/static/**`)

### 6.1 Constraints

- No build step. Use plain ES modules (`<script type="module" src="/static/app.js">`) and `<script src="https://telegram.org/js/telegram-web-app.js"></script>` in `<head>`, which must load first. There are no other third-party scripts, fonts or CDNs.
- **Size budget:** total JS under 60 KB and CSS under 15 KB, unminified is fine. The first paint shows a skeleton immediately. `/api/me` and `/api/home` are fetched in parallel after boot.
- Text is Uzbek Latin and hard-coded in the modules, since there is no i18n layer in v1. Use the bot's wording where it exists: "Navbatga yozilish", "Farqi yo'q", "Bajarildi", "Kelmadi", "Qabul qilish", "Rad etish", "Bekor qilish".
- Mobile-first, 360 px wide and up. Tap targets are at least 44 px. Respect `env(safe-area-inset-*)` and `Telegram.WebApp.contentSafeAreaInset` where available.
- No `localStorage` for anything important. The wizard state lives in memory only.

### 6.2 Theme (`style.css`)

Use only Telegram theme variables, with fallbacks:

```css
:root{
  --bg: var(--tg-theme-bg-color,#fff); --text: var(--tg-theme-text-color,#000);
  --hint: var(--tg-theme-hint-color,#999); --link: var(--tg-theme-link-color,#2481cc);
  --btn: var(--tg-theme-button-color,#2481cc); --btn-text: var(--tg-theme-button-text-color,#fff);
  --bg2: var(--tg-theme-secondary-bg-color,#f1f1f1); --section: var(--tg-theme-section-bg-color,var(--bg));
  --accent: var(--tg-theme-accent-text-color,var(--link)); --destructive: var(--tg-theme-destructive-text-color,#e53935);
  --sep: var(--tg-theme-section-separator-color,rgba(0,0,0,.08));
}
body{background:var(--bg2);color:var(--text);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0}
```

The `--tg-theme-*` variables are set by `telegram-web-app.js`, which handles light and dark. On `themeChanged`, call `Telegram.WebApp.setHeaderColor('secondary_bg_color')` and `setBackgroundColor(...)` again.

Components:
- section cards (`--section`, radius 12)
- list rows
- chips for the date strip and slots: selected uses `--btn`, disabled uses `--hint` at 40 % opacity
- status pills: PENDING amber, CONFIRMED green, others `--hint`
- bottom sheet for confirmations
- toast

### 6.3 Telegram glue (`tg.js`)

- On boot: `tg.ready()`, `tg.expand()`, `disableVerticalSwipes()` (7.7) and `enableClosingConfirmation()` only while the wizard has a selected slot.
- **Version gating** [J-C6]: every optional call goes through `tg.call(minVersion, fn)` which checks `isVersionAtLeast` and is a no-op otherwise, inside try/catch. `setHeaderColor` 6.1; `showPopup`, `showConfirm`, `enableClosingConfirmation` 6.2; `requestContact` 6.9; `disableVerticalSwipes` 7.7; `contentSafeAreaInset` 8.0. If `showConfirm` is unavailable, use the in-app bottom sheet.
- **Layout** [J-C4]: heights use `var(--tg-viewport-stable-height, 100vh)`, never bare `100vh`. Text-input screens (comment, name, reject reason) also show an inline "Davom etish" button and `blur()` the input on submit. On `viewportChanged`, scroll the focused field into view.
- **MainButton** is the only primary CTA:
  - `setMainButton({text, onClick, enabled, progress})` wraps `MainButton.setParams`, `onClick`/`offClick`, and `showProgress`/`hideProgress`.
  - Every screen calls `setMainButton(null)` on leave. `setMainButton` always `offClick`s the previous handler before binding a new one [J-C5].
  - It is disabled with progress while a POST is in flight, which prevents double taps.
- **BackButton:** shown on every screen except home. It pops the router stack. It is bound **once, in the router**; the router `offClick`s the old handler on every route change [J-C5].
- **HapticFeedback:**
  - `selectionChanged()` on chip select
  - `impactOccurred('light')` on navigation
  - `notificationOccurred('success'|'error'|'warning')` on booking created, error, or late-cancel warning
- **Confirmations:** `tg.showConfirm(text, cb)` for cancel, done and no-show, wrapped in try/catch (a second popup throws `WebAppPopupOpened`). Reject reasons use an **in-app bottom sheet** with 4 rows, Band / Kasal / Sababsiz / Boshqa…, where "Boshqa…" reveals a textarea. Never `showPopup` with more than 3 buttons [J-A7].
- **XSS:** `ui.js` `h()` sets text with `textContent` only. No `innerHTML` anywhere with server data (bio, comment, names, `consent_text`, error messages) [J-B8].
- **Links:** `tg.openLink(url)` for map links. Phones are plain `<a href="tel:…">`, never `openLink` [J-C7]. `tg.openTelegramLink(url)` for `t.me` links (referral share, open bot).
- **Share referral:** `tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(link))`, plus a copy button using `navigator.clipboard`.
- **No initData:** if `tg.initData` is empty or `tg.initDataUnsafe.user` is missing (opened outside Telegram or from a reply keyboard), show a full-screen note instead of the app: "Ilovani bot ichidagi «🌐 Ilovani ochish» tugmasi yoki menyu tugmasi orqali oching."

### 6.4 API client (`api.js`)

```js
export async function api(method, path, body) // -> data | throws ApiError {status, code, message, ctx}
// headers: Authorization: `tma ${Telegram.WebApp.initData}`, Content-Type: application/json
// timeout 15 s via AbortController; network error -> ApiError{code:'network', message:"Internet aloqasini tekshiring"}
// after a network error on POST /api/bookings: refetch GET /api/bookings first; if the booking exists, show success [J-D1]
```

Global handling of error codes:

| code | handling |
|---|---|
| `unauthorized` | full-screen "Sessiya eskirdi, ilovani qayta oching" + `tg.close()` button |
| `user_blocked` | full-screen blocked note, the bot's `err.user_blocked` |
| `consent_required` | go to the consent screen |
| `throttled` | toast "Biroz kuting" |
| anything else | returned to the caller; shown as toast or inline using `error.message` from the server |

### 6.5 Router and screens

The router is a hash-based stack in `app.js`: `#/home`, `#/book`, `#/bookings`, `#/booking/41`, `#/bonus`, `#/barbers`, `#/barber`. Each screen module exports `render(root, params) -> cleanup()`.

**Boot flow:**
1. `GET /api/me`.
2. If `consent_required`, show the **Consent** screen. Otherwise route to the hash, default `#/home`. If `start_param === "barber"` and the user is a barber, open `#/barber`.

**Consent** (`consent.js`)
- Shows `consent_text`.
- MainButton "✅ Roziman" → `POST /api/me/consent` → home.

**Home** (`home.js`): `GET /api/home`.
- Header: shop name (or "Sartaroshxona"), address (tap → `map_url` via openLink), phone (tel: link).
- Today's hours line, from `shop.hours[weekday]`.
- **Upcoming card** if `upcoming`: barber, service, `local.label`, `local.time`, status pill. Tap → `#/booking/{id}`. If `upcoming_count > 1`, show "Yana N ta navbat" → `#/bookings`.
- **Bonus tile**:
  - stamps mode: `✂️✂️✂️▫️▫️ 3/5`, "Bonus tayyor!" when `can_redeem`
  - cashback mode: balance in so'm
  - Tap → `#/bonus`.
- If `rebook`: a secondary button "🔁 Avvalgidek: {barber}, {service}" → `#/book?barber={id}&service={id}`.
- List rows: "📋 Navbatlarim" → `#/bookings`, "💈 Ustalar va narxlar" → `#/barbers`, "🎁 Bonuslar" → `#/bonus`, and "✂️ Usta paneli" → `#/barber` if `is_barber`.
- MainButton: **"Yozilish"** → `#/book`.

**Booking wizard** (`book.js`)

It is a single screen with steps. BackButton goes to the previous step. State is `{service_id, barber_id (null = any), barber_fixed, day, start_at, comment, use_bonus}`. Query params `barber` and `service` preselect, the same as the bot's `barber_first` and `rebook` entries.

1. **Service**
   - `GET /api/booking/services`. If `has_services` is false, skip this step.
   - List rows show name, "30 daq" and "60 000 so'm".
   - Tap selects and advances, with no MainButton.
   - If `barber_fixed`, show only the services that barber offers, taken from `GET /api/booking/barbers?service_id=` filtering. Simpler: validate on the next step and fall back to the full list.
2. **Barber**
   - `GET /api/booking/barbers?service_id=`.
   - The first row is "🤝 Farqi yo'q" with "{price} so'm dan" when `price_varies`, shown if `any.available`. It is followed by barber rows with photo, name and price.
   - Skip this step when it is preselected or there is only one barber.
3. **Date strip**
   - `GET /api/booking/days?…`.
   - A horizontal scroll of chips: "Bugun", "Ertaga", then `short` labels.
   - Days without free slots are disabled.
   - The first day with slots is auto-selected.
   - If no day has slots: "Bo'sh vaqt yo'q", with a button to choose another barber.
4. **Slot grid**, on the same screen under the strip:
   - `GET /api/booking/slots?…&day=` on every day change. Cancel the previous request with AbortController.
   - Three groups titled Ertalab / Kunduzi / Kechqurun, as a 4-column chip grid.
   - Selecting a slot shows the MainButton "Davom etish".
5. **Comment**
   - An optional textarea, max 300 characters with a counter, placeholder "Izoh (ixtiyoriy)".
   - MainButton "Davom etish".
6. **Phone gate**: `user.phone_verified` comes from `/api/me`, cached and refreshed after contact.
   - If false, show "Navbat uchun telefon raqamingiz kerak" with MainButton "📱 Raqamni yuborish".
   - The button calls `Telegram.WebApp.requestContact(cb)`.
   - If the callback gets `(true, res)` with `res.response`, call `POST /api/me/contact {response: res.response}` and continue.
   - If it gets `true` without a `response` (older clients), poll `GET /api/me` every 1.5 s up to 10 s until `phone_verified`. The bot handler in §2.5 saves the contact.
   - If it gets `false` or polling times out, show the text "Raqamni botga yuboring: chatdagi «📱 Raqamni yuborish» tugmasi" and a button to `openTelegramLink('https://t.me/'+bot_username)`.
   - If `requestContact` is not supported (`!tg.isVersionAtLeast('6.9')`), show the same text and button directly.
7. **Name gate**: if `user.name_ok` is false, show an input → `POST /api/me/name`.
8. **Confirm**: `POST /api/booking/quote`.
   - A summary card: barber (or "🤝 Farqi yo'q"), service and duration, `local.label` and time, and the price ("…dan" if `price_varies`).
   - If `bonus.possible`, a toggle "🎁 Bonusdan foydalanish (−{cost})". When it is on, show `final_price_uzs`, with the full price struck through.
   - If `needs_approval_hint`, the note "Usta tasdiqlashi kerak bo'ladi".
   - If `slot_free` is false, show a toast and go back to the slot grid.
   - MainButton "✅ Tasdiqlash" → `POST /api/bookings` with the `Idempotency-Key` generated when this screen opened (reused on retry).
   - If the confirm screen has been idle for more than 2 min, or the page becomes visible again (`visibilitychange`), requote before enabling the button [J-D3]. The slot grid refetches on `visibilitychange` too.
   - **On success:** haptic `success`, then a result screen:
     - auto-confirmed: "✅ Navbatingiz tasdiqlandi"
     - pending: "⏳ So'rov yuborildi. Usta tasdiqlagach xabar beramiz."
     - Show the booking card and the address. MainButton "Bosh sahifa".
     - Replace the stack so Back goes home.
   - **On error:** handle as in the table in §4.5.

**My bookings** (`bookings.js`): `GET /api/bookings`.
- Two segments, "Kelgusi" and "O'tgan", with cards.
- Tap → `#/booking/{id}`.
- If both lists are empty: "Hali navbat yo'q" and MainButton "Yozilish".

**Booking detail** (`booking.js`): `GET /api/bookings/{id}`.
- The full card, plus comment, price, bonus line, cancel reason and the original time for a proposal.
- Actions, driven only by `actions`:
  - `cancel`: a destructive button "Bekor qilish". It opens `showConfirm`, whose text adds "⚠️ Kech bekor qilish: {free_cancel_hours} soatdan kam qoldi" when `late_cancel_warning`. Then `POST …/cancel {withdraw:false}`. Toast: "Navbat bekor qilindi" or, when `late`, "Navbat bekor qilindi (kech)". Then refresh.
  - `withdraw`: "So'rovni qaytarib olish" → `POST …/cancel {withdraw:true}`.
  - `answer_proposal`: "Usta yangi vaqt taklif qildi: {local}". Buttons "✅ Roziman" and "❌ Yo'q" → `POST …/proposal`.
  - For finished bookings, "🔁 Yana yozilish" → `#/book?barber=&service=`.

**Bonus** (`bonus.js`): `GET /api/bonus`.
- If `bonus.enabled` is false: "Bonus dasturi hozir o'chirilgan".
- Stamps mode: a large stamp row with `progress/required`, the "{left} ta tashrifdan keyin bepul" text, and the reserved count if any.
- Cashback mode: the balance in so'm, plus the percent and max-redeem rules.
- History list: `reason_label`, delta (+green / −red) and the local date.
- Referral section if enabled: the link, "Nusxa olish" (copy) and "Ulashish" (share) → `t.me/share`. Explain "Do'stingiz birinchi tashrifidan keyin {bonus} bonus olasiz".

**Barbers** (`barbers.js`): `GET /api/barbers`.
- Cards with the photo (`<img loading="lazy">`, initials placeholder on error), name, bio and a service/price table.
- Button "Shu ustaga yozilish" → `#/book?barber={id}`.

**Barber panel** (`barber.js`): `GET /api/barber/days`, then `GET /api/barber/queue?day=`.
- The date strip (today selected) and a header "{label}: N ta navbat".
- PENDING items are first, highlighted. Then the rest by time.
- Each card: time, client name, phone (`tel:` link only when unmasked), service, comment, a stats line ("🆕 yangi mijoz" or "{visits} tashrif · {no_shows} kelmagan") and a status pill.
- `actions.accept`/`reject`: buttons "✅ Qabul qilish" → `…/accept`, and "❌ Rad etish" → reason bottom sheet → `…/reject`.
- `actions.done`/`no_show`: buttons "✅ Bajarildi" → confirm → `…/done`, and "🚫 Kelmadi" → confirm → `…/no-show`, disabled until `done_from` / `no_show_from`. Show the returned `notes` as a toast.
- After each action: haptic, then reload the day. A 409 `invalid_transition` reloads and shows the toast "Holat allaqachon o'zgargan".
- Pull-to-refresh is not needed. Add a refresh button in the header, and auto-refresh every 60 s while the screen is visible (`document.visibilityState`). If the response's `today` differs from the one shown, refetch `/api/barber/days` too [J-D4].
- Footer link "Boshqa amallar botda" → `openTelegramLink`.

---

## 7. Tests (QA, `tests/test_webapp_*.py`)

Use `aiohttp.test_utils.TestServer` and `TestClient` (no new dependency), real SQLite through the existing fixtures, a `FakeNotifier` that records calls, and `bot=None` (photos test monkeypatches).

`test_webapp_auth.py`:
- A valid signature passes. A bad hash, a missing hash, an expired `auth_date`, a future `auth_date` and a missing user all give 401.
- The signature helper builds initData with a test token.
- A new tg user gets a users row via `ensure_user`. `start_param=ref_<code>` sets `referrer_id`.
- A blocked user gets 403 `user_blocked`. Without consent, `/api/bookings` gets 403 `consent_required`, while `/api/me` returns 200.
- A non-barber on `/api/barber/*` gets 403.
- A `/api/me/contact` response is validated, and a foreign `user_id` gets 403.

`test_webapp_api.py`:
- Wizard reads: services, barbers, days and slots match the service functions.
- `POST /api/bookings` creates a PENDING booking and calls `FakeNotifier.booking_created` once, after commit, with the `BookingCreated` result.
- A second booking of the same slot by another user gets 409 `slot_taken`.
- Limits give 409 `client_limit`. No phone gives 422 `phone_required`.
- Cancel: a late cancel sets `late`, and the notifier gets `booking_cancelled(by_client=True)`. A second cancel returns 200 as a no-op, and the notifier is called exactly once (also with two concurrent requests).
- Barber accept, reject, done and no-show call the same notifier methods. Doing each twice returns 200 and the notifier is called exactly once. Barber X on barber Y's booking gets 403 and the DB is unchanged.
- User B cancelling user A's booking gets 403 and the DB is unchanged.
- `POST /api/bookings` replayed with the same `Idempotency-Key` returns the same 201 and one `booking_created`. Two concurrent creates by one user are serialised.
- `start_at` with `Z` parses (the code path is 3.10-safe); a non-grid second is rejected.
- A session with `start_param` does not call `ensure_user` on its second request.
- The rate limit triggers before any DB access (monkeypatch `resolve_user` to fail if called).
- A comment `<img src=x onerror=alert(1)>` round-trips unchanged as JSON text (the frontend renders via `textContent`).
- The `F.contact` router is after the FSM contact states and before `fallback`: a contact during `BookingFlow.contact` still reaches the booking handler.
- No response carries `Access-Control-Allow-Origin`.
- Error JSON shape, `ctx` serializability, and 500 hides details.

The existing 338 tests must still pass. The locale key parity test covers the new keys. The ruff check is clean.

---

## 8. Docs and deploy (BACKEND)

**README section "🌐 Mini App (web view)":**
1. **How it works:** the same process runs aiohttp on `WEBAPP_PORT`. It serves `app/webapp/static` and `/api`, and every request is signed by Telegram (`initData`, HMAC).
2. **BotFather:**
   - `/mybots` → bot → Bot Settings → Configure Mini App → enable, URL = `WEBAPP_URL`. This is optional, for a `t.me/<bot>/app` link and `startapp`.
   - The menu button is set by the bot itself at start.
   - `/setdomain` is not needed.
3. **Local dev:**

   ```
   WEBAPP_ENABLED=true
   python run.py --migrate
   cloudflared tunnel --url http://localhost:8080
   ```

   Then put the printed `https://….trycloudflare.com/` into `WEBAPP_URL` and restart. The quick-tunnel URL changes on every run, and old hub buttons keep the old URL. Use a named tunnel or a fixed domain in production [J-C2].
4. **Production:**
   - Run behind nginx or Caddy with HTTPS. Bind `WEBAPP_HOST=127.0.0.1` when the proxy is on the same host.
   - Caddy example: `shop.example.uz { reverse_proxy 127.0.0.1:8080 }`.
   - nginx `location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; }` plus certbot.
   - Never expose port 8080 publicly without TLS. Never log `$http_authorization` in nginx `log_format`. Set `client_max_body_size 64k`.
   - Set `WEBAPP_TRUST_PROXY=true` only when a proxy you control sets `X-Forwarded-For`.
5. **Troubleshooting:**
   - "Sessiya eskirdi": initData older than `WEBAPP_AUTH_MAX_AGE`. Reopen the app.
   - Buttons missing: `WEBAPP_URL` is empty or not https, or the server failed to bind (see the log).

**`docker-compose.yml`:** add the following under `bot`. The `Dockerfile` gets `EXPOSE 8080`.

```yaml
ports:
  - "127.0.0.1:8080:8080"   # Mini App; put a TLS reverse proxy in front
```

Set `WEBAPP_HOST=0.0.0.0` in the compose `environment:` (the code default is `127.0.0.1`) [J-B7]. That `ports:` mapping assumes the TLS proxy runs on the host. If Caddy or cloudflared runs as a compose service, drop `ports:` and use `reverse_proxy bot:8080` over the compose network.

Run **one replica only**: `bot.lock` lives under `data_path` and only protects a shared volume.

**Configuration table in README:** add the seven `WEBAPP_*` variables.

**Bot token:** lives only in `.env` as `BOT_TOKEN`. It never appears in this spec, README, compose files, tests or logs.

---

## 9. Acceptance checklist

- [ ] `WEBAPP_ENABLED=false` (default): behaviour is identical to today, and no port is opened.
- [ ] Enabled with a URL: the chat menu button opens the app, and the hub shows "🌐 Ilovani ochish" (inline web_app).
- [ ] A client books in the web app. The barber gets the same decision message with Accept/Reject/Propose, and accepting it in Telegram updates the booking in the web app after refresh.
- [ ] A booking made in the bot appears in the web app, and cancelling it in the web app notifies the barber with the same text as the bot. The late-cancel policy is identical.
- [ ] A barber accepts or rejects in the web app. Every Telegram copy of the decision message is edited, and the client is notified.
- [ ] A slot race returns 409 `slot_taken`, and the UI returns to the slot grid.
- [ ] A user without a phone can share it with `requestContact`, or via the bot fallback.
- [ ] Light and dark themes look native. MainButton and BackButton work, and haptics fire.
- [ ] Blocked users and users without consent are handled. Tampered initData gets 401.
- [ ] The token is never logged, and initData is never logged.
- [ ] Double taps, two open copies, and Telegram-then-app decisions never produce a second notification.
- [ ] Tested on Android, iOS and Telegram Desktop: open from the menu button and the hub button, Back button, keyboard on the comment screen.

---

## 10. Judge rulings (final, binding)

The critic's claims were checked against the code: `bookings.py` (`confirm_booking`, `reject_booking`, `complete_booking`, `mark_no_show` and `cancel_by_client` return quietly when the transition was already applied), `notifier.py:309` (`source == BookingSource.BOT`), `middlewares/user.py` (`or payload`), `i18n.fmt_date` (`"26-oktabr, dushanba"`), `pyproject.toml` (`>=3.10`), `uow.write_tx` (SQLite `BEGIN IMMEDIATE`), and `users.ensure_user` (only `ref_`/`src_` payloads act). The body above has already been changed to match these rulings; the tags `[J-xx]` mark the edits.

**Legend:** ACCEPT means it is in v1 as stated. MODIFY means it is in v1 with the changes given. DEFER means it is not in v1.

| # | Ruling | Decision |
|---|---|---|
| A1 | ACCEPT | Barber writes take a `before` snapshot via `get_booking_for_actor` **inside the same write tx** (serialised by `BEGIN IMMEDIATE` on SQLite) and run under a per-booking in-process `asyncio.Lock`. Notify only when the status changed. "409 already decided" is removed; a repeat is 200 with no notification. On PostgreSQL a web-vs-bot race is still possible between the unlocked snapshot and `_load_locked`. This is the same exposure the bot has today, and we accept it for v1. |
| A2 | ACCEPT | Cancel and proposal follow the same pattern. The test becomes "second cancel is a no-op, notifier called once". |
| A3 | ACCEPT | Web bookings stay `BookingSource.BOT` (`create_booking` hard-codes it). No new enum value. A `channel` column is deferred until someone needs analytics. |
| A4 | ACCEPT | `replace("Z", "+00:00")`, require tz, convert to UTC, reject non-zero seconds or microseconds. |
| A5 | ACCEPT | `resolve_user` writes only when `ctx is None or needs_sync(...)`. |
| A6 | ACCEPT | Per-`tg_id` rate limit right after HMAC, before the DB. 401s are limited per IP. |
| A7 | ACCEPT | Reject reasons go in an in-app bottom sheet. Popups are wrapped in try/catch. |
| A8 | ACCEPT | Labels are exactly `fmt_date(d, "uz")`. The example is fixed. |
| B1 | MODIFY | `max_age` is 86400 for GET and 3600 for POST (`WEBAPP_AUTH_MAX_AGE_WRITE`). Expiry shows "Sessiya eskirdi" and offers `tg.close()`. No headers in logs; the nginx note is added. **DEFER** the `/api/session` token exchange to v1.1. |
| B2 | ACCEPT | 4 KB cap, duplicate keys rejected, hash lowercased. |
| B3 | ACCEPT | Keep both layers (handler and service). IDOR tests added. Admins get the barber view of `GET /api/bookings/{id}` (with phones), the same as the bot; this is intended. `barber_id` is never read from the request on `/api/barber/*`. |
| B4 | ACCEPT | Verified: `ensure_user` ignores unknown payloads such as `barber`, and `start_param` now reaches it only for new or changed users anyway. `is_barber` is a boolean only. |
| B5 | MODIFY | Negative cache for 10 min, 60/min per IP, `X-Forwarded-For` only with `WEBAPP_TRUST_PROXY`, cache 30 × 1 MB, `?v=` ignored. **DEFER** "store the smallest photo size ≥ 320 px". It changes the bot's photo-upload handler and is out of scope for this iteration. |
| B6 | ACCEPT | No CORS headers ever; JSON content type required on POST. CSRF does not apply (header auth). |
| B7 | ACCEPT | Default `WEBAPP_HOST=127.0.0.1`; compose sets `0.0.0.0`. Both proxy layouts are documented. |
| B8 | ACCEPT | CSP gets `base-uri`/`form-action`/`object-src 'none'`. `textContent` only, including `consent_text`. |
| B9 | ACCEPT | `phone_number` is required. The `invalid_phone` foreign rule comes from `set_verified_contact` unchanged. |
| C1 | ACCEPT | Also treat a missing `initDataUnsafe.user` as "no initData". Manual test on Android, iOS and Desktop is in §9. |
| C2 | ACCEPT | Docs only. The hub is already re-rendered on `/start`. |
| C3 | ACCEPT | Use `res.response`. The chat handler stays silent if that exact phone was already verified. Router-order test added. |
| C4 | MODIFY | Stable viewport height, inline "Davom etish" plus `blur()` on text screens, and scroll into view. `disableVerticalSwipes` stays **on at boot** (gated to 7.7), not toggled per focus. That is simpler, and the app is a scrolling app. |
| C5 | ACCEPT | The BackButton is bound once in the router. MainButton and BackButton always `offClick` the old handler. |
| C6 | ACCEPT | A single `tg.call(minVersion, fn)` guard. |
| C7 | ACCEPT | Plain `<a href="tel:">`. |
| D1 | ACCEPT | `Idempotency-Key` header (required on `POST /api/bookings`), 10 min in-memory replay map, per-user lock, refetch after a network error. |
| D2 | ACCEPT | `booking_created` is awaited inline. Other notifications use tracked tasks, drained on `stop()` with a 10 s timeout. |
| D3 | ACCEPT | Refetch on `visibilitychange`, requote after 2 min idle, the success screen shows the price from the 201, and "any" prices are shown with "…dan". |
| D4 | ACCEPT | `clock.local_today()` everywhere, `today` is in the barber responses, and `day` is range-checked (client: horizon; barber: today … +20). |
| D5 | ACCEPT | `actions.accept` covers EXPIRED within grace. `done_from` and `no_show_from` drive disabled buttons. The windows mirror the service (−30 min / +15 min); the service stays the authority. |
| D6 | ACCEPT | `tel:` only when the phone has no `*`. |
| E | ACCEPT | Relative URLs only, `client_max_body_size 64k`, `/healthz` without a version, one replica only. The menu button is reset only if it is our `MenuButtonWebApp` ("Ilova"); one set by hand in BotFather is left alone. |
| Token | ACCEPT | The bot token belongs only in `.env` as `BOT_TOKEN` (the line already exists there; the value is not repeated here or anywhere else). Because it was pasted in plain chat, **rotate it with BotFather `/revoke` after setup** and update `.env`. |

**Scope guard for this iteration:** no `/api/session` tokens, no photo re-encoding or resizing, no `channel` column, no admin or propose/staff-cancel screens in the web app, no i18n layer in the frontend. Everything else in §1–§9 is in scope.

**Shared helpers added to the module list** (BACKEND, `app/webapp/`):
- `locks.py`: `booking_lock(id)` and `user_lock(tg_id)` (a `WeakValueDictionary` of `asyncio.Lock`), and the `Idempotency-Key` TTL map.
- `tasks.py`: `_PENDING` set, `spawn(coro)`, `drain_pending_notifications(timeout)`.
- `ratelimit.py`: the sliding-window limiter, used for per-`tg_id` and per-IP limits.
