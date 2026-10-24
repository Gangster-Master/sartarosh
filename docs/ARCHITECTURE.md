# Sartarosh Navbat Bot: architecture (final, binding contract)

**Status:** FINAL, issued by the Judge on 2026-10-24. This document replaces the CTO draft. It folds in the Judge's rulings on every critic point (PLAN.md §8) and the adopted GitHub ideas (IDEAS.md §12). BACKEND, BOT/FRONT and QA build against this text. A signature change follows PLAN.md §7: BACKEND proposes, the CTO approves, and the stubs, DTOs and QA parity list change in the same PR.

Tags used in this document:
- **[P2]** marks an MVP item that sits behind the cut line (IDEAS.md §12). Its schema, DTOs and service signatures ship on day 1 in every case. Only the implementation and UI may be cut to v1.1.
- **[Cx]** refers to a critic finding in PLAN.md §8.
- **[#n]** refers to an idea in IDEAS.md.

---

## 0. Decisions

### 0.1 Build our own
We build our own bot. The research found no well-maintained project that is good enough to adopt (RND.md §2). The MIT repos (Xaveron, DigitalJacob, noXplode) are references. We write the code ourselves. If a file is copied or closely adapted, its MIT notice goes into `THIRD_PARTY_NOTICES` in the same PR. Nothing is copied from unlicensed repos.

### 0.2 Platform
- **Code target: Python 3.10.12**, the version installed here. Do not use `datetime.UTC` (use `timezone.utc`), `enum.StrEnum` (use `class X(str, Enum)` and always `.value`), `typing.Self` or `ExceptionGroup`. Ruff `target-version = "py310"`.
- **Production runtime:** the Docker image `python:3.12-slim` is recommended, because Python 3.10 reaches end of life in October 2026 [G6]. The code must run on both 3.10 and 3.12, and CI runs on both.
- **Database:** SQLite in WAL mode, one process, for one shop. The schema and SQL stay portable to PostgreSQL 16 (v1.1 option). No SQLite-only SQL, and no `DATE()` on timestamp columns.
- **Double-booking guard:** the `booking_slots` cell table (§3.3) with a primary-key conflict. It behaves the same on SQLite and Postgres.
- **Single instance:** the bot takes an exclusive `fcntl.flock` on `<DATA_DIR>/bot.lock` at startup and exits with code 3 if the lock is held [A4].
- **Hosting:** the bot is deployable to an Uzbekistan host (UzCloud or a local VPS) for ZRU-547 data localisation [H1]. The owner chooses the host (PLAN.md §10, Q9).

### 0.3 Pinned dependencies (`requirements.txt`, owned by BACKEND)
```
aiogram==3.31.0
SQLAlchemy[asyncio]==2.0.55      # NOT 2.1.x (needs 3.11+)
aiosqlite==0.22.1
alembic>=1.19,<1.20              # NOT 1.20.x yet (released 2026-10-22)
pydantic-settings==2.15.0        # NOT 2.16.x (needs 3.11+)
APScheduler==3.11.3              # NOT 4.x
tzdata>=2025.2                   # required: slim images have no zoneinfo db [C2]
# optional
redis==8.1.0                     # only if REDIS_URL is used for FSM
# dev (requirements-dev.txt)
pytest==9.1.*
pytest-asyncio==1.4.0
ruff
```
`pydantic` is not pinned directly; aiogram constrains it. On day 1, BE-01 installs into a clean 3.10 venv and a clean 3.12 venv and freezes the result into `requirements.lock`, which is the deployable set [G6].

### 0.4 Defaults for the open questions (requirements §11)
| # | Question | MVP default (all changeable in ⚙️ Sozlamalar unless noted) |
|---|---|---|
| Q1 | Manual confirm? | **Auto-accept is the default** (`barbers.auto_accept=True`) [B3]. A booking still goes to PENDING when the client is new (`new_client_requires_approval=True`) or soft-blocked, or when the barber has auto-accept turned off. |
| Q2 | Service required? | Services are optional. With none configured, the service step is skipped and the duration is `barber.slot_step_min`. The seed adds "Soch olish" (30 min), "Soqol" (15 min) and "Soch + soqol" (60 min) [#13]. |
| Q3 | Step / buffer | The step is 30 min (one of 15/30/45/60). The buffer `barbers.buffer_min` is one of 0/15/30, default 0 [#28]. |
| Q4 | Bonus type | Stamp card, where 5 stamps make the 6th visit free (`loyalty_mode="stamps"`). Cashback mode is available [P2][#42]. |
| Q5 | Horizon | 7 days. |
| Q6 | Barber days off | Barbers may block their own time (`barbers_can_block_time=True`). |
| Q7 | No-show policy | 3 no-shows in 90 days means the client's bookings need manual approval. There are no deposits. |
| Q8 | Branches | One shop. `shop_id=1` columns are kept. |

---

## 1. Ownership and contract rules

| Path | Owner | Rule |
|---|---|---|
| `app/config.py`, `app/db/**`, `app/services/**`, `app/ops/**`, `migrations/**`, `alembic.ini`, `tools/**`, `deploy/**` | **BACKEND** | Must not import `aiogram`. |
| `app/bot/**` (including `locales/`) | **BOT/FRONT** | May import `app.config`, `app.services.*`, `app.services.dto`, `app.services.errors`, `app.db.enums`, `app.db.uow` and `app.db.session`. Never imports `app.db.models`. Never builds SQL. |
| `tests/**` | **QA** | Services are tested against real SQLite. Bot tests are smoke tests. |
| root (`requirements*.txt`, `requirements.lock`, `.env.example`, `README.md`, `pyproject.toml`, `THIRD_PARTY_NOTICES`) | BACKEND, PM review | |

**Binding rules:**
1. Every service function is `async def f(session: AsyncSession, ...)`. Everything after `session` is keyword-only, except a single obvious id where shown. Services return DTOs from `app/services/dto.py`, never ORM objects. Pure helpers (`clock`, `validation`, `slots.compute_free_slots`) are sync and take no session.
2. Services call `flush()` and `begin_nested()`, but **never `commit()` or `rollback()`**. The transaction is owned by `app/db/uow.py` (§5.4).
3. **No network I/O inside a transaction [A2].** A bot handler opens `write_tx`/`read_tx`, calls services, leaves the block (which commits or rolls back), and only then talks to Telegram. The scheduler does the same.
4. Expected failures raise subclasses of `ServiceError` (§4.1). Any exception that escapes `write_tx` rolls the whole transaction back, so no orphan rows are left [A1]. The bot maps `err.code` to `t(lang, "err.<code>", **err.ctx)`.
5. Every `datetime` that crosses the boundary is **aware UTC**. Services raise `ValueError` on a naive value. A `date` always means a local calendar day in `SHOP_TZ`. Local-day grouping always goes through `clock.day_bounds_utc` [C6].
6. Every function that depends on time takes `now: datetime | None = None`, where `None` means `clock.now_utc()`. Tests always pass `now`.
7. **Permissions are checked inside services**, on reads too [D5]. A function that takes `actor_user_id` loads the actor's roles itself. Bot handlers never call the internal functions marked `# internal` in §4. CI greps `app/bot/handlers` for them.
8. Barber role and admin role are re-read from the DB on every update and never cached, so unlinking or demoting someone takes effect at once [H2].

---

## 2. Directory tree

```
sartarosh/
├── requirements.txt  requirements-dev.txt  requirements.lock
├── pyproject.toml              # ruff (py310) + pytest (asyncio_mode="auto", testpaths=tests)
├── alembic.ini
├── .env.example
├── README.md
├── THIRD_PARTY_NOTICES
├── data/                       # gitignored: bot.db, bot.lock, backups/
├── deploy/
│   ├── Dockerfile              # python:3.12-slim, non-root, CMD python -m app.bot.main
│   └── sartarosh.service       # systemd: Restart=always, WorkingDirectory, EnvironmentFile
├── tools/
│   └── backup.sh               # wrapper: python -m app.ops.backup  (+ prints restore command)
├── migrations/
│   ├── env.py                  # async alembic env; render_as_batch=True for sqlite
│   └── versions/0001_baseline.py
├── app/
│   ├── __init__.py
│   ├── config.py               # BACKEND
│   ├── db/                     # BACKEND
│   │   ├── __init__.py
│   │   ├── base.py             # DeclarativeBase, naming convention, TimestampMixin
│   │   ├── types.py            # UTCDateTime, StrEnumType
│   │   ├── enums.py            # all enums (§3.1)
│   │   ├── models.py           # all ORM models (§3)
│   │   ├── session.py          # create_engine(), create_sessionmaker(), sqlite listeners
│   │   ├── uow.py              # write_tx(), read_tx()  (§5.4)
│   │   ├── migrate.py          # `python -m app.db.migrate` = alembic upgrade head; assert_schema_current()
│   │   └── seed.py             # `python -m app.db.seed --demo`
│   ├── ops/
│   │   ├── __init__.py
│   │   └── backup.py           # sqlite online backup API, keep 14  (`python -m app.ops.backup`)
│   ├── services/               # BACKEND, no aiogram
│   │   ├── __init__.py
│   │   ├── errors.py  dto.py  clock.py  validation.py
│   │   ├── settings.py         # ShopSettings + job markers + setup token
│   │   ├── users.py            # users, roles, consent, contact, referral codes, admin client ops, delete_me
│   │   ├── barbers.py          # barber CRUD, invite + approval linking, reachability
│   │   ├── catalog.py          # services + barber_services
│   │   ├── schedule.py         # working hours, time off, busy-now, conflicts, shop hours
│   │   ├── slots.py            # pure algorithm + DB wrappers + "any barber"
│   │   ├── bookings.py         # create, state machine, proposals, queries
│   │   ├── bonus.py            # ledger (stamps | cashback), welcome, referral
│   │   ├── notifications.py    # outbox: schedule / claim / mark
│   │   ├── reviews.py          # [P2] rating 1–5
│   │   ├── fsm_store.py        # key/value persistence for the bot FSM storage
│   │   ├── stats.py
│   │   └── audit.py
│   └── bot/                    # BOT/FRONT
│       ├── __init__.py
│       ├── main.py             # entry: `python -m app.bot.main`
│       ├── i18n.py             # t(lang, key, **kw), labels(key), fmt_* helpers, h()
│       ├── locales/
│       │   ├── __init__.py
│       │   ├── uz.py           # STRINGS: dict[str, str]  (Uzbek Latin, default, P1)
│       │   └── ru.py           # STRINGS: dict[str, str]  [P2]; same key set
│       ├── callbacks.py        # CallbackData factories (§7)
│       ├── states.py           # StatesGroups (§8)
│       ├── filters.py          # IsAdmin, IsBarber, IsPrivate
│       ├── screen.py           # render(): edit-in-place with text/photo switching (§6.7)
│       ├── fsm_storage.py      # DbStorage(BaseStorage) over services.fsm_store
│       ├── notifier.py         # all outgoing pushes; never inside a tx
│       ├── scheduler.py        # one tick job; daily jobs driven by markers
│       ├── middlewares/
│       │   ├── __init__.py
│       │   ├── private.py      # drop non-private updates; leave groups
│       │   ├── throttle.py     # messages + callbacks, TTL buckets
│       │   ├── debounce.py     # identical callback within 1.0 s → answered and dropped
│       │   └── user.py         # user/roles/lang injection, consent gate, blocked drop
│       ├── keyboards/
│       │   ├── __init__.py
│       │   ├── common.py  client.py  barber.py  admin.py  calendar.py
│       └── handlers/
│           ├── __init__.py     # build_root_router()
│           ├── errors.py       # dp.errors handler
│           ├── onboarding.py   # /start payloads, lang, consent, /whoami, /delete_me, /help, /menu, /cancel
│           ├── client_booking.py
│           ├── client_my.py    # my bookings, cancel/withdraw, reminder ack, proposal answers, manual-visit confirm
│           ├── client_info.py  # hub, barbers & prices, barber card, address & hours, bonus page, referral
│           ├── client_feedback.py   # [P2] rating
│           ├── barber_decide.py     # accept / reject / propose
│           ├── barber_queue.py      # today, upcoming, done, no-show, staff cancel
│           ├── barber_block.py      # BlockTimeFlow + busy-now
│           ├── barber_manual.py     # ManualBookingFlow
│           ├── admin_barbers.py  admin_services.py  admin_hours.py  admin_timeoff.py
│           ├── admin_bookings.py admin_clients.py   admin_settings.py admin_stats.py
│           ├── admin_admins.py      # admin list, promote/demote, link approvals
│           └── fallback.py
└── tests/                      # QA (see §10)
```

---

## 3. Database schema (`app/db/models.py`)

General rules:
- Integer PKs autoincrement. Telegram ids are `BigInteger`.
- Every datetime column is `UTCDateTime`. It stores naive UTC on SQLite and `timestamptz` on PG, returns aware UTC, and raises on naive input.
- Enum columns use `StrEnumType(EnumCls, length)`. It stores `.value` in a `String(n)` and returns the enum member [G8].
- `MetaData(naming_convention=...)`: `ix_%(column_0_label)s`, `uq_%(table_name)s_%(column_0_N_name)s`, `ck_%(table_name)s_%(constraint_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `pk_%(table_name)s`.
- The schema is created **only by Alembic** [G7]. `0001_baseline` matches `models.py`, and `alembic check` must report no diff (test_migrations). Tests may use `metadata.create_all` for speed.
- All `start_at`/`end_at`/`block_end_at` values and all time-off bounds lie on the **15-minute grid**. `SLOT_UNIT_MIN = 15`. The shop timezone's UTC offset must be a whole multiple of 15 minutes, which is checked at startup, so the local grid and the UTC grid coincide.

### 3.1 Enums (`app/db/enums.py`)
```python
class BookingStatus(str, Enum):
    PENDING="PENDING"; CONFIRMED="CONFIRMED"; COMPLETED="COMPLETED"; NO_SHOW="NO_SHOW"
    REJECTED="REJECTED"; EXPIRED="EXPIRED"
    CANCELLED_BY_CLIENT="CANCELLED_BY_CLIENT"; CANCELLED_BY_BARBER="CANCELLED_BY_BARBER"
ACTIVE_STATUSES: frozenset[BookingStatus] = frozenset({BookingStatus.PENDING, BookingStatus.CONFIRMED})

class Awaiting(str, Enum):        BARBER="barber"; CLIENT="client"          # only meaningful while PENDING
class BookingSource(str, Enum):   BOT="bot"; MANUAL="manual"
class CancelledBy(str, Enum):     CLIENT="client"; BARBER="barber"; ADMIN="admin"; SYSTEM="system"
class LoyaltyMode(str, Enum):     STAMPS="stamps"; CASHBACK="cashback"
class BonusReason(str, Enum):
    EARN="earn"; REDEEM="redeem"; WELCOME="welcome"; REFERRAL="referral"
    MANUAL="manual"; REVERSAL="reversal"
class NotificationKind(str, Enum):
    PENDING_REPING="rping"      # barber + admins re-ping with decision buttons
    REMINDER_24H="r24"          # client, with Kelaman / Bekor qilish
    REMINDER_2H="r2"            # client; skipped if client_ack_at is set
    RETURN="ret"                # "time for a haircut" [#37]
    REVIEW="rev"                # [P2] rating request [#54]
    MANUAL_VISIT_CONFIRM="mcf"  # "Bugun Ali'da edingizmi?" for manual bookings [D2]
```

### 3.2 Tables

**users**
| column | type | constraints / meaning |
|---|---|---|
| id | int | PK |
| tg_id | bigint | UNIQUE, NOT NULL. After `/delete_me` it is set to `-id`. |
| tg_username | str(64) | null |
| first_name | str(128) | not null, default '' |
| full_name | str(64) | null. The name used on bookings (validated). |
| phone | str(16) | null, INDEX |
| phone_verified | bool | default false. True only when it came from the user's own `request_contact`. |
| lang | str(5) | default 'uz'. One of 'uz', 'ru'. |
| lang_chosen | bool | default false |
| consent_version | int | null. The version of the privacy notice accepted [#73]. |
| is_admin | bool | default false. **Promoted in DB only.** Env admins are computed at runtime [G2]. |
| is_blocked | bool | default false |
| no_show_count | int | default 0 (lifetime, for display) |
| late_cancel_count | int | default 0 |
| no_show_reset_at | UTCDateTime | null |
| bonus_balance | int | default 0. Cache equal to `SUM(bonus_transactions.delta)`, updated atomically (`SET bonus_balance = bonus_balance + :d`) [A3]. |
| ref_code | str(12) | UNIQUE, not null. 8 chars from `[a-z0-9]`, generated on create [D6]. |
| referrer_id | int FK users.id SET NULL | null. Set only on create, never to self. |
| acq_source | str(32) | null. From `/start src_<tag>` [#78]. |
| deleted_at | UTCDateTime | null |
| created_at, updated_at | UTCDateTime | |

**barbers**
| column | type | constraints |
|---|---|---|
| id | int | PK |
| shop_id | int | default 1 |
| user_id | int FK users SET NULL | null, UNIQUE |
| display_name | str(64) | not null |
| photo_file_id | str(256) | null |
| bio | str(300) | null |
| is_active | bool | default true |
| sort_order | int | default 0 |
| slot_step_min | int | default 30, CHECK IN (15,30,45,60) |
| buffer_min | int | default 0, CHECK IN (0,15,30) |
| auto_accept | bool | **default true** |
| invite_token | str(32) | null, UNIQUE |
| invite_expires_at | UTCDateTime | null |
| pending_link_user_id | int FK users SET NULL | null. A link request waiting for admin approval [D4]. |
| is_reachable | bool | default true |
| created_at, updated_at | | |

A barber is **public** (shown to clients and bookable by them) when `is_active AND user_id IS NOT NULL` [B4].

**services**: `id, shop_id=1, name str(64), duration_min int CHECK (>0 AND %15=0 AND <=240), price_uzs int CHECK >=0, is_active bool=true, sort_order int=0, created_at, updated_at`.

**barber_services**: PK(`barber_id` FK CASCADE, `service_id` FK CASCADE), `price_uzs int null` CHECK >=0, `duration_min int null` (same CHECK as services when not null). If a barber has no rows, they offer all active services.

**working_hours**: `id, barber_id FK CASCADE, weekday int CHECK 0..6 (Mon=0), start_time Time, end_time Time CHECK start<end, break_start Time null, break_end Time null`, UNIQUE(barber_id, weekday). All times lie on 15-minute marks, and a break satisfies `start <= break_start < break_end <= end` (checked in the service). No row means a day off.

**time_off**: `id, shop_id=1, barber_id FK CASCADE null (null = shop-wide), start_at, end_at CHECK end>start, reason str(200) null, created_by_user_id FK users SET NULL null, created_at`, INDEX(barber_id, start_at).

**bookings**
| column | type | constraints / meaning |
|---|---|---|
| id | int | PK |
| shop_id | int | default 1 |
| client_id | FK users SET NULL | null for an unlinked walk-in |
| barber_id | FK barbers RESTRICT | not null |
| service_id | FK services SET NULL | null |
| service_name_snapshot | str(64) | null |
| start_at, end_at | UTCDateTime | CHECK end_at > start_at |
| block_end_at | UTCDateTime | `end_at + barber.buffer_min` (snapshot). The cells cover `[start_at, block_end_at)`. |
| duration_min | int | snapshot |
| price_snapshot | int | not null |
| bonus_cost | int | default 0. Reserved redemption (stamps or so'm) snapshotted at creation [#44][A5]. |
| final_price_uzs | int | price after the discount (provisional until COMPLETED) |
| bonus_redeemed | bool | default false. Set on COMPLETED when the redemption succeeded. |
| status | str(20) | INDEX |
| awaiting | str(8) | null. `Awaiting` while PENDING. |
| proposal_count | int | default 0, CHECK <= 2 [#21] |
| original_start_at | UTCDateTime | null. The client's requested time before the first proposal. |
| source | str(8) | `bot` or `manual` |
| client_name | str(64) | not null |
| client_phone | str(16) | null |
| client_phone_verified | bool | default false |
| comment | str(300) | null |
| expire_at | UTCDateTime | null. Used only while PENDING. |
| late_cancel | bool | default false |
| client_ack_at | UTCDateTime | null |
| auto_completed | bool | default false |
| completed_at | UTCDateTime | null |
| visit_confirmed | bool | null. For manual bookings: the client's answer to `mcf`. |
| cancelled_by | str(8) | null |
| cancel_reason | str(200) | null |
| decided_at | UTCDateTime | null |
| created_by_user_id | FK users SET NULL | null (the actor for manual bookings) |
| created_at, updated_at | | |
| | | INDEX(barber_id, start_at), INDEX(client_id, start_at), INDEX(client_phone), INDEX(status, expire_at) |

**booking_slots**: the DB-level double-booking guard
| column | type | constraints |
|---|---|---|
| barber_id | int FK barbers CASCADE | |
| slot_start | UTCDateTime | on the 15-minute grid |
| booking_id | int FK bookings CASCADE | INDEX |
| | | **PRIMARY KEY (barber_id, slot_start)** |

**Invariant:** a booking in `ACTIVE_STATUSES` owns exactly one row for each 15-minute cell in `[start_at, block_end_at)`. A booking in any other status owns none. All cell changes go through two private helpers in `bookings.py`:
- `_occupy(session, booking)` runs inside `begin_nested()` and maps `IntegrityError` to `SlotTakenError`.
- `_release(session, booking_id)` deletes the booking's rows.

Every transition calls one of them in the same transaction. QA asserts the cell count after every transition.

**booking_messages**: the decision messages that may need editing later.
`id, booking_id FK CASCADE, chat_id bigint, message_id int, role str(8) ('barber'|'admin'), created_at`, UNIQUE(chat_id, message_id), INDEX(booking_id).

**scheduled_notifications** (the outbox)
| column | type | constraints |
|---|---|---|
| id | int | PK |
| booking_id | FK bookings CASCADE | not null |
| kind | str(8) | NotificationKind |
| send_at | UTCDateTime | INDEX |
| claimed_at | UTCDateTime | null. Claimed means it will not be sent again (at most once) [A4]. |
| sent_at | UTCDateTime | null |
| cancelled_at | UTCDateTime | null |
| attempts | int | default 0 |
| last_error | str(200) | null |
| | | **UNIQUE(booking_id, kind)** |

**bonus_transactions** (an append-only ledger)
| column | type | constraints |
|---|---|---|
| id | int | PK |
| user_id | FK users CASCADE | not null, INDEX |
| booking_id | FK bookings SET NULL | null |
| delta | int | CHECK delta <> 0 |
| unit | str(8) | `LoyaltyMode` value at the time of writing |
| reason | str(10) | BonusReason |
| earn_day | Date | null. Set only for `earn`: the local day of the visit. |
| comment | str(200) | null. Required for `manual`. |
| created_by_user_id | FK users SET NULL | null |
| created_at | | |
| | | **UNIQUE(user_id, booking_id, reason)**: no double earn, redeem or referral per booking (NULL booking_ids are distinct) |
| | | **UNIQUE(user_id, earn_day)**: at most one earn per client per local day [D2] |

**reviews** [P2]: `booking_id PK FK CASCADE, user_id FK users SET NULL, barber_id FK barbers CASCADE, rating int CHECK 1..5, created_at`.

**fsm_states**: `key str(128) PK, state str(128) null, data Text (JSON) not null default '{}', updated_at`. Rows untouched for 7 days are purged by the tick.

**settings**: `key str(64) PK, value Text (JSON), updated_at`. Keys that start with `_` are internal (job markers, setup token) and are never shown in the admin UI.

**audit_log**: `id, actor_user_id FK users SET NULL null, action str(64), entity str(32), entity_id int null, payload Text (JSON) null, created_at`, INDEX(created_at).

### 3.3 `app/db/session.py`
```python
def resolve_database_url(raw: str | None) -> str
    # relative sqlite paths are resolved against PROJECT_ROOT (Path(__file__).parents[2]), and the
    # parent dir is created with mkdir(parents=True, exist_ok=True) [G4]
def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine
def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]   # expire_on_commit=False, autoflush=True
```
SQLite listeners on `engine.sync_engine`:
- **On `connect`:** `dbapi_conn.isolation_level = None`, then `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000` and `synchronous=NORMAL`.
- **On `begin`:**
  ```python
  mode = conn.get_execution_options().get("sqlite_begin", "DEFERRED")   # "IMMEDIATE" for write_tx
  conn.exec_driver_sql(f"BEGIN {mode}")
  ```
- For `:memory:`, use `poolclass=StaticPool` and `connect_args={"check_same_thread": False}`.

RND-01 verifies on day 2 that the execution options set through `session.connection(execution_options=...)` are visible in the `begin` event. If they are not, the fallback is two engines on the same file: a write engine that always runs `BEGIN IMMEDIATE`, and a read engine that runs a deferred `BEGIN`. `uow.py` hides which one is used.

### 3.4 `app/db/uow.py` (transaction ownership)
```python
@asynccontextmanager
async def write_tx(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]
    # opens a session, BEGIN IMMEDIATE on SQLite (FOR UPDATE semantics on PG are taken by services),
    # yields; commit on normal exit; rollback on ANY exception (including ServiceError), then re-raise
@asynccontextmanager
async def read_tx(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]
    # deferred BEGIN; always rollback on exit (read-only by convention; a flush inside raises in debug mode)
```
A bot handler or job must keep each block short. No `await bot.*` call may appear inside it. Code review enforces this, and so does the `tests/test_bot_smoke.py` AST check: no `bot.`, `call.`, `message.` or `notifier.` attribute calls inside an `async with write_tx/read_tx` block in `app/bot/**`.

### 3.5 `app/db/migrate.py`
```python
def upgrade_head(url: str | None = None) -> None          # CLI: python -m app.db.migrate
async def assert_schema_current(engine: AsyncEngine) -> None
    # raises RuntimeError("schema not at head: run python -m app.db.migrate") — main.py refuses to start [#94]
```

### 3.6 `app/db/seed.py`
`python -m app.db.seed --demo` refuses to run without `--demo`. It also refuses if any barber already exists, unless `--force-demo` is passed [G3]. It creates:
- 3 barbers (unlinked, with invite links printed). The seed **does not** link them, so they stay hidden until linked.
- The 3 services from §0.4.
- Hours Mon–Sat 10:00–20:00 with a 13:00–14:00 break.
- Demo shop info.

The production first run uses the admin wizard and no seed.

---

## 4. Service layer: exact signatures

Common imports: `from sqlalchemy.ext.asyncio import AsyncSession`, `from datetime import date, datetime, time`.

### 4.1 `app/services/errors.py`
```python
class ServiceError(Exception):
    code: str = "error"
    def __init__(self, message: str = "", **ctx: object) -> None: ...   # self.ctx = ctx (used as i18n kwargs)

class NotFoundError(ServiceError):          code = "not_found"
class PermissionDeniedError(ServiceError):  code = "forbidden"
class InputError(ServiceError):             code = "invalid"          # ctx: field=   (NOT "ValidationError") [G8]
class InvalidPhoneError(InputError):        code = "invalid_phone"
class InvalidNameError(InputError):         code = "invalid_name"
class PhoneRequiredError(ServiceError):     code = "phone_required"   # bot booking without verified phone [D1]
class SlotTakenError(ServiceError):         code = "slot_taken"
class SlotUnavailableError(ServiceError):   code = "slot_unavailable"
class BarberInactiveError(ServiceError):    code = "barber_inactive"  # inactive or not public
class ClientLimitError(ServiceError):       code = "client_limit"     # ctx: kind="max_active"|"max_per_day"|"overlap", limit=int
class UserBlockedError(ServiceError):       code = "user_blocked"
class InvalidTransitionError(ServiceError): code = "invalid_transition"  # ctx: current=status value
class TooEarlyError(ServiceError):          code = "too_early"
class TooLateError(ServiceError):           code = "too_late"         # e.g. no-show flip window passed, grace passed
class InsufficientBonusError(ServiceError): code = "no_bonus"
class InvalidInviteError(ServiceError):     code = "invalid_invite"
class ProposalLimitError(ServiceError):     code = "proposal_limit"
class ConsentRequiredError(ServiceError):   code = "consent_required"
```
CI test: every `code` has a key `err.<code>` in every locale.

### 4.2 `app/services/dto.py`
Every DTO is `@dataclass(frozen=True, slots=True)`.
```python
class Roles: is_client: bool; is_barber: bool; is_admin: bool; is_env_admin: bool; barber_id: int | None

class UserDTO: id: int; tg_id: int; tg_username: str | None; first_name: str; full_name: str | None
    display_name: str                       # full_name or first_name
    phone: str | None; phone_verified: bool; lang: str; lang_chosen: bool; consent_version: int | None
    is_admin: bool                          # promoted-in-DB flag only
    is_blocked: bool; no_show_count: int; late_cancel_count: int; bonus_balance: int
    ref_code: str; referrer_id: int | None; created_at: datetime

class UserContext: user: UserDTO; roles: Roles; barber_reachable: bool | None   # for UserMiddleware

class BarberDTO: id: int; user_id: int | None; tg_id: int | None; display_name: str
    photo_file_id: str | None; bio: str | None; is_active: bool; is_public: bool; sort_order: int
    slot_step_min: int; buffer_min: int; auto_accept: bool; is_reachable: bool
    invite_token: str | None; invite_expires_at: datetime | None
    pending_link_user_id: int | None

class LinkRequest: barber: BarberDTO; user: UserDTO

class ServiceDTO: id: int; name: str; duration_min: int; price_uzs: int; is_active: bool; sort_order: int
class ResolvedService: service_id: int | None; name: str | None; duration_min: int; price_uzs: int

class WorkingHoursDTO: barber_id: int; weekday: int; start_time: time; end_time: time
    break_start: time | None; break_end: time | None
class ShopDayHours: weekday: int; open_time: time | None; close_time: time | None   # union over public barbers
class TimeOffDTO: id: int; barber_id: int | None; start_at: datetime; end_at: datetime; reason: str | None

class Interval: start: datetime; end: datetime          # aware UTC, half-open
class DayAvailability: day: date; is_working: bool; has_free_slots: bool

class BookingDTO: id: int; status: BookingStatus; awaiting: Awaiting | None; source: BookingSource
    client_id: int | None; client_tg_id: int | None; client_lang: str
    client_name: str; client_phone: str | None       # MASKED for barbers on old bookings [H2]
    client_phone_verified: bool
    barber_id: int; barber_name: str; barber_tg_id: int | None
    service_id: int | None; service_name: str | None
    start_at: datetime; end_at: datetime; duration_min: int
    original_start_at: datetime | None; proposal_count: int
    price_snapshot: int; final_price_uzs: int; bonus_cost: int; bonus_redeemed: bool
    comment: str | None; expire_at: datetime | None; late_cancel: bool; client_ack_at: datetime | None
    auto_completed: bool; completed_at: datetime | None; visit_confirmed: bool | None
    cancelled_by: CancelledBy | None; cancel_reason: str | None
    created_at: datetime

class DecisionTarget: chat_id: int; lang: str; role: str        # "barber" | "admin"
class BookingCreated: booking: BookingDTO; auto_confirmed: bool; client_soft_blocked: bool
    decision_targets: list[DecisionTarget]      # who gets accept/reject (barber, or admins if barber unreachable)
class CancelResult: booking: BookingDTO; late: bool
class CompleteResult: booking: BookingDTO; earned: int; redeemed: bool; bonus_failed: bool
    new_balance: int; welcome_credited: int; referral_credited_to: int | None   # referrer user id
    needs_visit_confirm: bool                   # manual booking: stamp waits for client "Ha"
class NoShowResult: booking: BookingDTO; reversed: int          # bonus reversed for an auto-completed flip
class ProposalResult: booking: BookingDTO                      # awaiting=CLIENT, start_at = proposed
class ClientBookings: upcoming: list[BookingDTO]; past: list[BookingDTO]
class ClientStats: total_completed: int; no_show_count: int; late_cancel_count: int; is_new: bool
class RebookHint: barber_id: int; barber_name: str; service_id: int | None; service_name: str | None

class BonusTxDTO: id: int; delta: int; unit: LoyaltyMode; reason: BonusReason; comment: str | None; created_at: datetime
class BonusStatus: mode: LoyaltyMode; enabled: bool; balance: int; reserved: int; available: int
    required: int; progress: int; can_redeem: bool       # stamps: progress = min(available, required)
    reward_type: str; reward_value: int                   # stamps mode
    cashback_percent: int; max_redeem_percent: int        # cashback mode
    recent: list[BonusTxDTO]
class RedeemQuote: possible: bool; cost: int; final_price_uzs: int; discount_uzs: int

class ContactResult: user: UserDTO; is_foreign: bool; duplicate_user_ids: list[int]
class TimeOffResult: time_off: TimeOffDTO; cancelled: list[BookingDTO]; overlapping_kept: list[BookingDTO]
class HoursChangeResult: hours: WorkingHoursDTO | None; out_of_hours: list[BookingDTO]

class DueNotification: id: int; kind: NotificationKind; booking: BookingDTO
    targets: list[DecisionTarget]               # rping: barber (+admins); others: empty (client from booking)
class PendingDigestItem: booking: BookingDTO; targets: list[DecisionTarget]

class ClientProfile: user: UserDTO; stats: ClientStats; recent_bookings: list[BookingDTO]
    bonus: BonusStatus; soft_blocked: bool
class Page(Generic[T]): items: list[T]; page: int; pages: int; total: int   # page is 0-based
class ReviewResult: booking_id: int; rating: int; is_low: bool             # [P2] low = rating <= 3

class BarberStatsRow: barber_id: int; barber_name: str; total: int; completed: int; auto_completed: int
    manual_completed: int; no_show: int; cancelled: int; rejected: int; expired: int; revenue_uzs: int
class ServiceStatsRow: service_name: str; completed: int; revenue_uzs: int
class StatsDTO: start: date; end: date; rows: list[BarberStatsRow]; totals: BarberStatsRow
    top_services: list[ServiceStatsRow]; new_clients: int

class ShopSettings: (one typed field per key in §4.4 DEFAULTS, same names)
```
Day 1, M0 gate: BACKEND delivers `enums.py`, `errors.py`, `dto.py` and stubs for **every** signature in §4 that raise `NotImplementedError`.

### 4.3 `app/services/clock.py` (pure)
```python
SLOT_UNIT_MIN: int = 15
def get_tz() -> ZoneInfo                         # lru_cache; ZoneInfo(get_config().shop_tz) — lazy, never at import [C3]
def assert_tz_on_grid() -> None                  # RuntimeError if utcoffset % 15 min != 0; called by main.py
def now_utc() -> datetime
def to_local(dt: datetime) -> datetime
def local_dt(day: date, t: time) -> datetime     # → aware UTC
def local_today(now: datetime | None = None) -> date
def day_bounds_utc(day: date) -> tuple[datetime, datetime]
def ensure_aware(dt: datetime) -> datetime       # ValueError if naive
def is_on_grid(dt: datetime) -> bool
def floor_to_grid(dt: datetime) -> datetime
def ceil_to_grid(dt: datetime) -> datetime
def clamp_to_daytime(dt: datetime, *, start: time = time(10), end: time = time(21)) -> datetime
    # moves dt forward into [start, end) local; used for ret/rev sends
```

### 4.4 `app/services/settings.py`
```python
DEFAULTS: dict[str, object] = {
  # booking
  "booking_horizon_days": 7, "min_lead_min": 30, "free_cancel_hours": 2,
  "max_active_bookings": 3, "max_requests_per_day": 5,
  # pending / decision
  "pending_timeout_min": 60, "pending_cutoff_before_start_min": 10, "min_decision_min": 15,
  "pending_reping_min": 15, "proposal_reply_min": 30, "late_accept_grace_min": 10,
  "new_client_requires_approval": True,
  # reminders / retention
  "reminder_24h_enabled": True, "reminder_2h_enabled": True,
  "return_reminder_weeks": 4, "reviews_enabled": True, "review_delay_hours": 2,
  # no-show policy
  "late_cancels_per_no_show": 0, "soft_block_no_shows": 3, "soft_block_window_days": 90,
  "auto_complete_after_hours": 12, "noshow_flip_window_hours": 24,
  # loyalty
  "loyalty_enabled": True, "loyalty_mode": "stamps",
  "bonus_required_stamps": 5, "bonus_reward_type": "free", "bonus_reward_value": 100,
  "cashback_percent": 5, "cashback_max_redeem_percent": 50,
  "welcome_bonus": 1, "referral_enabled": True, "referral_bonus": 1,
  # staff
  "barbers_can_block_time": True, "allow_foreign_phone": True,
  # jobs (local time "HH:MM")
  "daily_summary_time": "08:00", "nightly_check_time": "23:00", "backup_time": "03:30",
  # shop info
  "shop_name": "", "address": "", "latitude": None, "longitude": None, "contact_phone": "",
  "map_yandex_url": "", "map_google_url": "", "map_2gis_url": "",
}
SETTING_SPECS: dict[str, SettingSpec]   # type (int|bool|str|time|float|choice), min, max, choices — drives parsing + admin UI
async def get_shop_settings(session: AsyncSession) -> ShopSettings
async def update_setting(session: AsyncSession, *, actor_user_id: int, key: str, raw_value: str) -> ShopSettings
    # admin only; parse per spec → InputError(field=key)
    # cross-field: min_lead_min >= pending_cutoff_before_start_min + min_decision_min [B2]
    # loyalty_mode change: only if every users.bonus_balance == 0 and no ACTIVE booking has bonus_cost > 0
    #   → InputError(field="loyalty_mode", reason="balances_exist")
    # audit "setting.update"
async def set_location(session: AsyncSession, *, actor_user_id: int, latitude: float, longitude: float) -> ShopSettings
async def get_marker(session: AsyncSession, key: str) -> str | None             # internal "_job.*"
async def set_marker(session: AsyncSession, key: str, value: str) -> None       # internal
async def ensure_setup_token(session: AsyncSession) -> str | None
    # internal; if no admin exists (env ADMIN_IDS empty AND no users.is_admin) create "_setup_token"
    # (token_urlsafe(16)) and return it; else delete it and return None [G1]
```

### 4.5 `app/services/validation.py` (pure)
```python
PHONE_RE = re.compile(r"^\+998\d{9}$")
def normalize_phone(raw: str) -> str
    # strip spaces - ( ) ; '+998XXXXXXXXX' | '998XXXXXXXXX' | 'XXXXXXXXX' → '+998XXXXXXXXX'; else InvalidPhoneError
def normalize_contact_phone(raw: str) -> tuple[str, bool]     # request_contact value → ('+digits', is_foreign)
def validate_name(raw: str) -> str
    # strip, collapse spaces; 2..50 chars; letters (str.isalpha, any script), space, ' ʻ ʼ ` -; else InvalidNameError
def validate_comment(raw: str) -> str | None
    # strip; '' → None; drop control chars; > 300 chars → InputError(field="comment")
def mask_phone(phone: str) -> str                               # '+998 90 *** ** 67'
```

### 4.6 `app/services/users.py`
```python
async def load_context(session: AsyncSession, *, tg_id: int) -> UserContext | None   # read-only
def needs_sync(user: UserDTO, *, tg_username: str | None, first_name: str) -> bool   # pure [A2]
async def ensure_user(session: AsyncSession, *, tg_id: int, tg_username: str | None, first_name: str,
                      language_code: str | None = None, start_payload: str | None = None) -> UserContext
    # upsert. On create: lang = 'ru' if language_code startswith 'ru' else 'uz'; ref_code generated;
    #   start_payload "ref_<code>" → referrer_id (never self; ignored if unknown) [D6];
    #   "src_<tag>" → acq_source (tag [a-z0-9_]{1,32}).
    # On existing: refresh username/first_name only if changed. Never changes referrer.
async def get_user(session: AsyncSession, user_id: int) -> UserDTO                  # NotFoundError
async def get_roles(session: AsyncSession, user_id: int) -> Roles
    # is_env_admin = tg_id in config.admin_ids; is_admin = is_env_admin or users.is_admin;
    # is_barber = a barbers row with user_id and is_active
async def set_lang(session: AsyncSession, *, user_id: int, lang: str) -> UserDTO     # 'uz'|'ru'
async def accept_consent(session: AsyncSession, *, user_id: int, version: int) -> UserDTO
async def set_full_name(session: AsyncSession, *, user_id: int, name: str) -> UserDTO
async def set_verified_contact(session: AsyncSession, *, user_id: int, raw_phone: str) -> ContactResult
    # bot has already checked contact.user_id == from_user.id. Foreign → InvalidPhoneError unless
    # allow_foreign_phone. Sets phone_verified=True. duplicate_user_ids = other users with same phone.
async def is_soft_blocked(session: AsyncSession, user_id: int, *, now: datetime | None = None) -> bool
    # NO_SHOW bookings with start_at >= max(now - window, no_show_reset_at)
    #   + (late_cancel_count // late_cancels_per_no_show if that setting > 0)  >= soft_block_no_shows
async def get_client_stats(session: AsyncSession, user_id: int) -> ClientStats     # is_new = total_completed == 0
async def claim_setup_token(session: AsyncSession, *, user_id: int, token: str) -> UserDTO
    # token == settings "_setup_token" → users.is_admin=True, delete token, audit; else InvalidInviteError
async def delete_me(session: AsyncSession, *, user_id: int, now: datetime | None = None) -> list[BookingDTO]
    # cancels the user's ACTIVE bookings (CANCELLED_BY_CLIENT, reason "deleted"), returns them;
    # anonymises: tg_id=-id, tg_username/full_name/phone=NULL, first_name='', deleted_at=now;
    # bookings.client_name='—', client_phone=NULL for all the user's bookings; keeps counts and ledger [H1]
# admin
async def find_users_by_phone(session: AsyncSession, *, actor_user_id: int, phone_query: str) -> list[UserDTO]
    # admin only; digits suffix match (>= 4 digits); max 10; audit "client.search" [H2]
async def get_client_profile(session: AsyncSession, *, actor_user_id: int, user_id: int) -> ClientProfile
async def set_blocked(session: AsyncSession, *, actor_user_id: int, user_id: int, blocked: bool,
                      now: datetime | None = None) -> list[BookingDTO]
    # admin only; cannot block self, an admin or a barber (PermissionDeniedError) [#69];
    # blocking cancels the user's ACTIVE bookings (CANCELLED_BY_BARBER, cancelled_by=ADMIN, reason "blocked") [D1]
async def reset_no_shows(session: AsyncSession, *, actor_user_id: int, user_id: int, now: datetime | None = None) -> UserDTO
async def set_admin(session: AsyncSession, *, actor_user_id: int, user_id: int, is_admin: bool) -> UserDTO
    # admin only; env admins cannot be demoted here (InputError(field="env_admin")); cannot demote self
async def list_admins(session: AsyncSession, *, actor_user_id: int) -> list[UserDTO]   # env + promoted
async def list_admin_chat_ids(session: AsyncSession) -> list[tuple[int, str]]          # internal: (tg_id, lang)
```

### 4.7 `app/services/barbers.py`
```python
async def list_barbers(session: AsyncSession, *, public_only: bool = True) -> list[BarberDTO]
    # public_only: is_active AND user_id IS NOT NULL; order sort_order, id
async def get_barber(session: AsyncSession, barber_id: int) -> BarberDTO
async def get_barber_by_user(session: AsyncSession, user_id: int) -> BarberDTO | None
async def create_barber(session: AsyncSession, *, actor_user_id: int, display_name: str, bio: str | None = None,
                        photo_file_id: str | None = None, slot_step_min: int = 30, buffer_min: int = 0,
                        auto_accept: bool = True, now: datetime | None = None) -> BarberDTO
    # invite_token = secrets.token_urlsafe(12) (16 chars); expires now + 24h [D4]
async def update_barber(session: AsyncSession, *, actor_user_id: int, barber_id: int,
                        display_name: str | None = None, bio: str | None = None, photo_file_id: str | None = None,
                        slot_step_min: int | None = None, buffer_min: int | None = None,
                        auto_accept: bool | None = None, sort_order: int | None = None) -> BarberDTO
async def regenerate_invite(session: AsyncSession, *, actor_user_id: int, barber_id: int,
                            now: datetime | None = None) -> BarberDTO
async def request_link(session: AsyncSession, *, user_id: int, token: str, now: datetime | None = None) -> LinkRequest
    # InvalidInviteError if unknown/expired/barber already linked/user already a barber.
    # Clears token, sets pending_link_user_id. Bot then asks admins to approve [D4].
async def approve_link(session: AsyncSession, *, actor_user_id: int, barber_id: int, user_id: int) -> BarberDTO
    # admin only; pending_link_user_id must equal user_id (else InvalidInviteError); sets user_id, is_reachable=True
async def deny_link(session: AsyncSession, *, actor_user_id: int, barber_id: int, user_id: int) -> BarberDTO
async def unlink_barber(session: AsyncSession, *, actor_user_id: int, barber_id: int) -> BarberDTO
async def preview_future_bookings(session: AsyncSession, *, actor_user_id: int, barber_id: int,
                                  now: datetime | None = None) -> list[BookingDTO]
async def deactivate_barber(session: AsyncSession, *, actor_user_id: int, barber_id: int, reason: str,
                            now: datetime | None = None) -> list[BookingDTO]
    # always cancels all future ACTIVE bookings (CANCELLED_BY_BARBER, cancelled_by=ADMIN) and returns them [B6]
async def activate_barber(session: AsyncSession, *, actor_user_id: int, barber_id: int) -> BarberDTO
async def set_reachable(session: AsyncSession, *, barber_id: int, reachable: bool) -> None   # internal
async def mark_reachable_by_user(session: AsyncSession, *, user_id: int) -> bool
    # internal; if that user's barber is unreachable → set True and return True [B4]
```

### 4.8 `app/services/catalog.py`
```python
async def list_services(session: AsyncSession, *, active_only: bool = True) -> list[ServiceDTO]
async def list_barber_services(session: AsyncSession, barber_id: int) -> list[ResolvedService]
async def list_bookable_services(session: AsyncSession) -> list[ServiceDTO]
    # active services offered by at least one public barber (for service-first flow) [#1]
async def list_barbers_for_service(session: AsyncSession, *, service_id: int | None) -> list[BarberDTO]   # public only
async def resolve_service(session: AsyncSession, *, barber_id: int, service_id: int | None) -> ResolvedService
    # None → (None, None, barber.slot_step_min, 0); not offered/inactive → InputError(field="service")
async def create_service(session: AsyncSession, *, actor_user_id: int, name: str, duration_min: int, price_uzs: int) -> ServiceDTO
async def update_service(session: AsyncSession, *, actor_user_id: int, service_id: int, name: str | None = None,
                         duration_min: int | None = None, price_uzs: int | None = None,
                         is_active: bool | None = None, sort_order: int | None = None) -> ServiceDTO
    # services are never deleted, only deactivated [#74]
async def set_barber_services(session: AsyncSession, *, actor_user_id: int, barber_id: int, service_ids: list[int]) -> list[ResolvedService]
async def set_barber_service_override(session: AsyncSession, *, actor_user_id: int, barber_id: int, service_id: int,
                                      price_uzs: int | None, duration_min: int | None) -> ResolvedService
    # MVP: service exists and is tested; admin UI for it is v1.1 [#14]
async def shop_has_services(session: AsyncSession) -> bool
```

### 4.9 `app/services/schedule.py`
```python
async def get_week_hours(session: AsyncSession, barber_id: int) -> list[WorkingHoursDTO]
async def get_shop_week_hours(session: AsyncSession) -> list[ShopDayHours]          # 7 rows, Mon..Sun [#60]
async def set_working_hours(session: AsyncSession, *, actor_user_id: int, barber_id: int, weekday: int,
                            start_time: time | None, end_time: time | None,
                            break_start: time | None = None, break_end: time | None = None,
                            now: datetime | None = None) -> HoursChangeResult
    # admin only; None start → day off. Returns future ACTIVE bookings now outside hours, NOT cancelled.
async def copy_hours_to_all_weekdays(session: AsyncSession, *, actor_user_id: int, barber_id: int,
                                     source_weekday: int, include_sunday: bool = False,
                                     now: datetime | None = None) -> list[BookingDTO]
async def list_time_off(session: AsyncSession, *, barber_id: int | None, start: datetime, end: datetime) -> list[TimeOffDTO]
async def preview_time_off_conflicts(session: AsyncSession, *, actor_user_id: int, barber_id: int | None,
                                     start_at: datetime, end_at: datetime) -> list[BookingDTO]
async def add_time_off(session: AsyncSession, *, actor_user_id: int, barber_id: int | None,
                       start_at: datetime, end_at: datetime, reason: str | None,
                       cancel_conflicts: bool, now: datetime | None = None) -> TimeOffResult
    # bounds floored/ceiled to grid. Barber: own barber_id only and only if barbers_can_block_time.
    # Shop-wide (None): admin only. cancel_conflicts → CANCELLED_BY_BARBER with reason; else overlapping_kept.
async def busy_now(session: AsyncSession, *, actor_user_id: int, minutes: int,
                   now: datetime | None = None) -> TimeOffResult
    # minutes in {30, 60}; actor must be a barber; [floor(now), ceil(now+minutes)); cancels nothing;
    # overlapping_kept lists bookings it overlaps (bot warns) [F1]
async def delete_time_off(session: AsyncSession, *, actor_user_id: int, time_off_id: int) -> None
def full_day_interval(day: date) -> Interval                                         # pure (sync)
```

### 4.10 `app/services/slots.py`
Pure algorithm [#6]:
```python
def compute_free_slots(*, day: date, hours: WorkingHoursDTO | None, blocks: list[Interval],
                       duration_min: int, buffer_min: int, step_min: int, now: datetime,
                       min_lead_min: int, last_bookable_day: date) -> list[datetime]
```
```
if hours is None or day < local_today(now) or day > last_bookable_day: return []
ws, we = local_dt(day, hours.start_time), local_dt(day, hours.end_time)
blocks = blocks + [break interval if set]
earliest = ceil_to_grid(now + min_lead_min)
cands = {ws + k*step for k >= 0 while < we}  ∪  {b.end for b in blocks if ws <= b.end < we}     # [F4]
out = []
for t in sorted(cands):
    occ_end = t + duration + buffer
    if t >= earliest and t + duration <= we and not any(overlap(t, occ_end, b.start, b.end) for b in blocks):
        out.append(t)
return out          # aware UTC, ascending, all on grid
```
Blocks are half-open, and `overlap(a0,a1,b0,b1) = a0 < b1 and b0 < a1`. The booking blocks the DB wrapper supplies are `[start_at, block_end_at)`, which includes the buffer. The buffer may run past closing time. Overnight shifts are not supported.

DB wrappers:
```python
async def get_free_slots(session: AsyncSession, *, barber_id: int, service_id: int | None, day: date,
                         now: datetime | None = None) -> list[datetime]
async def get_free_slots_any(session: AsyncSession, *, service_id: int | None, day: date,
                             now: datetime | None = None) -> list[datetime]
    # sorted union over list_barbers_for_service(service_id) [#1]
async def get_available_days(session: AsyncSession, *, barber_id: int | None, service_id: int | None,
                             now: datetime | None = None) -> list[DayAvailability]
    # today .. today + horizon - 1; barber_id None = any barber
async def is_slot_free(session: AsyncSession, *, barber_id: int, start_at: datetime, duration_min: int,
                       buffer_min: int, now: datetime | None = None, ignore_lead_time: bool = False,
                       ignore_booking_id: int | None = None) -> bool
async def list_proposal_slots(session: AsyncSession, *, booking_id: int, actor_user_id: int, limit: int = 8,
                              now: datetime | None = None) -> list[datetime]
    # free starts for the booking's barber+duration on its day, then the next 2 days, excluding its current
    # start, ignoring its own cells; lead time applies [#21]
```

### 4.11 `app/services/bookings.py`
```python
async def create_booking(session: AsyncSession, *, client_user_id: int, barber_id: int | None,
                         service_id: int | None, start_at: datetime, comment: str | None = None,
                         use_bonus: bool = False, now: datetime | None = None) -> BookingCreated
async def create_manual_booking(session: AsyncSession, *, actor_user_id: int, barber_id: int,
                                service_id: int | None, start_at: datetime, client_name: str,
                                client_phone: str | None, comment: str | None = None,
                                now: datetime | None = None) -> BookingDTO
async def add_decision_message(session: AsyncSession, *, booking_id: int, chat_id: int, message_id: int,
                               role: str) -> None                                   # internal (notifier)
async def list_decision_messages(session: AsyncSession, booking_id: int) -> list[tuple[int, int, str]]  # internal
async def confirm_booking(session: AsyncSession, *, booking_id: int, actor_user_id: int,
                          now: datetime | None = None) -> BookingDTO
async def reject_booking(session: AsyncSession, *, booking_id: int, actor_user_id: int, reason: str | None,
                         now: datetime | None = None) -> BookingDTO
async def propose_time(session: AsyncSession, *, booking_id: int, actor_user_id: int, new_start_at: datetime,
                       now: datetime | None = None) -> ProposalResult
async def answer_proposal(session: AsyncSession, *, booking_id: int, client_user_id: int, accept: bool,
                          now: datetime | None = None) -> BookingDTO
async def cancel_by_client(session: AsyncSession, *, booking_id: int, client_user_id: int,
                           reason: str | None = None, now: datetime | None = None) -> CancelResult
async def cancel_by_staff(session: AsyncSession, *, booking_id: int, actor_user_id: int, reason: str,
                          now: datetime | None = None) -> BookingDTO
async def complete_booking(session: AsyncSession, *, booking_id: int, actor_user_id: int,
                           now: datetime | None = None) -> CompleteResult
async def mark_no_show(session: AsyncSession, *, booking_id: int, actor_user_id: int,
                       now: datetime | None = None) -> NoShowResult
async def client_ack(session: AsyncSession, *, booking_id: int, client_user_id: int,
                     now: datetime | None = None) -> BookingDTO
async def confirm_manual_visit(session: AsyncSession, *, booking_id: int, client_user_id: int, yes: bool,
                               now: datetime | None = None) -> CompleteResult
async def expire_due_pending(session: AsyncSession, *, now: datetime | None = None) -> list[BookingDTO]     # internal
async def auto_complete_due(session: AsyncSession, *, now: datetime | None = None) -> list[CompleteResult]  # internal
async def get_booking(session: AsyncSession, booking_id: int) -> BookingDTO          # internal, no actor check
async def get_booking_for_actor(session: AsyncSession, *, booking_id: int, actor_user_id: int,
                                now: datetime | None = None) -> BookingDTO
    # allowed: the booking's client, the booking's barber, or an admin; else PermissionDeniedError [D5].
    # Barber (non-admin) sees client_phone masked when start_at < local_today - 1 day [H2].
async def list_client_bookings(session: AsyncSession, *, client_user_id: int, past_limit: int = 5,
                               now: datetime | None = None) -> ClientBookings
async def get_last_completed(session: AsyncSession, *, client_user_id: int) -> RebookHint | None   # [#15]
    # None if that barber is no longer public or service inactive (service → None then)
async def list_barber_day(session: AsyncSession, *, actor_user_id: int, barber_id: int, day: date,
                          include_inactive: bool = False, now: datetime | None = None) -> list[BookingDTO]
    # own barber or admin; default statuses PENDING, CONFIRMED, COMPLETED, NO_SHOW
async def list_bookings(session: AsyncSession, *, actor_user_id: int, day: date | None = None,
                        barber_id: int | None = None, status: BookingStatus | None = None,
                        phone_or_name: str | None = None, page: int = 0, page_size: int = 10) -> Page[BookingDTO]
    # admin only [#76]
async def list_unresolved(session: AsyncSession, *, barber_id: int | None = None,
                          now: datetime | None = None) -> list[BookingDTO]            # internal: CONFIRMED, end_at < now
async def list_pending_digest(session: AsyncSession, *, day: date,
                              now: datetime | None = None) -> list[PendingDigestItem] # internal: PENDING awaiting barber, start on day
async def bookings_for_daily_summary(session: AsyncSession, *, day: date) -> dict[int, list[BookingDTO]]  # internal
async def compute_expire_at(session: AsyncSession, *, barber_id: int, created_at: datetime,
                            start_at: datetime) -> datetime                           # internal (exported for tests)
```

**`create_booking` steps.** Everything runs in the caller's `write_tx`.
1. Check `start_at` with `ensure_aware` and `is_on_grid`, else raise `SlotUnavailableError`. Clean the comment with `validate_comment`.
2. Load the user. Raise `UserBlockedError` if blocked, and `ConsentRequiredError` if `consent_version` is below the current `CONSENT_VERSION` from `app.config`. Raise `PhoneRequiredError` if `phone` is missing or `phone_verified` is false [D1]. The name is `user.display_name`, and it must pass `validate_name`, otherwise raise `InvalidNameError`, which makes the bot ask for a name.
3. Lock the client: `SELECT … FROM users WHERE id=:u FOR UPDATE` on PG (a no-op on SQLite, where `BEGIN IMMEDIATE` already serialises) [A3].
4. Client limits, counted over bookings `WHERE client_id = :u OR client_phone = :phone` [D1]. Each failure raises `ClientLimitError`:
   - ACTIVE future bookings must stay below `max_active_bookings` (kind `max_active`).
   - Bookings created today (local day, any status, source=bot) must stay below `max_requests_per_day` (kind `max_per_day`).
   - No ACTIVE booking of this client may overlap `[start_at, start_at + duration)` (kind `overlap`). Adjacent bookings with the same barber are allowed, so families can book back to back [E5].
5. Pick the barber. If `barber_id` is given, it must be public, otherwise raise `BarberInactiveError`, and the candidate list is `[barber_id]`. If `barber_id` is None ("Farqi yo'q" [#1]), the candidates are the public barbers offering the service whose slot is free, ordered by (number of ACTIVE bookings that local day ascending, `sort_order`, `id`). An empty list raises `SlotTakenError`.
6. For each candidate, inside `begin_nested()`, run steps a–g. On `SlotTakenError`, move to the next candidate. After the last one, re-raise.
   - a. `rs = resolve_service(...)`. Compute `end_at = start_at + rs.duration_min` and `block_end_at = end_at + barber.buffer_min`.
   - b. Run `is_slot_free(...)`. If it returns False, raise `SlotTakenError` when an ACTIVE booking overlaps, otherwise `SlotUnavailableError`, which ends the loop.
   - c. Bonus [#44]. If `use_bonus`: `q = bonus.quote_redeem(session, user_id=u, price_uzs=rs.price_uzs)`. If `not q.possible`, raise `InsufficientBonusError`. Otherwise `bonus_cost = q.cost` and `final_price = q.final_price_uzs`. Without a bonus, `bonus_cost = 0` and `final = price`.
   - d. Status. `soft = is_soft_blocked(...)` and `new = stats.is_new`. The booking is CONFIRMED when `barber.auto_accept and not soft and not (new and new_client_requires_approval) and barber.is_reachable`. Otherwise it is PENDING with `awaiting=BARBER` and `expire_at = compute_expire_at(...)`.
   - e. Insert the booking with snapshots, `client_phone_verified=True` and `source=bot`.
   - f. `_occupy(...)` inserts the cells.
   - g. Break out of the loop.
7. `notifications.schedule_for_booking(...)`.
8. Build `decision_targets`. For PENDING: the barber, if the barber is reachable and linked. Admins are added when the barber is unreachable, has no linked user, or has auto-accept on but the booking needed approval anyway (new or soft-blocked client) [#23]. For CONFIRMED the list is empty.
9. Return `BookingCreated`. A typed phone never overwrites a verified one, because bot bookings use only the profile phone [D3].

**`create_manual_booking` rules [#26]:**
- The actor must be the linked user of `barber_id`, or an admin.
- `source=MANUAL` and `status=CONFIRMED`.
- `start_at` must be at least `floor_to_grid(now) - 15 min`. Lead time and client limits do not apply.
- `client_phone` goes through `normalize_phone` when given.
- `client_id` is set only if **exactly one** user has `phone == client_phone AND phone_verified` [D3]. Otherwise it is NULL.
- The service writes an audit entry `booking.manual_create`.
- No client pushes are sent for manual bookings, except `mcf` after completion.

**`compute_expire_at` [B1][B2]:**
```
cutoff = start_at - pending_cutoff_before_start_min            # default 10
if created_at is inside the barber's working window (local day of created_at, outside break): base = created_at
else: base = next working-window start strictly after created_at and before start_at, or None
if base is None or base >= cutoff:  expire_at = cutoff          # barber gets the whole time (morning digest covers it)
else:                               expire_at = min(base + pending_timeout_min, cutoff)
expire_at = max(expire_at, created_at + min_decision_min)       # default 15; never instant
expire_at = min(expire_at, start_at)                            # never after start
```
`update_setting` enforces `min_lead_min >= cutoff + min_decision_min`, so a decision window of at least 15 minutes always exists. Worked examples (barber 10:00–20:00) are in §10 `test_pending_expiry`.

**State machine.** Every transition not in the table raises `InvalidTransitionError(current=…)`. A repeat of the same action on a booking that is already in the target state returns the DTO unchanged, which makes the call idempotent.

| from | to | via | actor | side effects |
|---|---|---|---|---|
| PENDING(awaiting=BARBER) | CONFIRMED | confirm_booking | barber/admin | `decided_at`; cancel `rping`; schedule reminders |
| EXPIRED (within `late_accept_grace_min` of `expire_at`, start_at > now, was awaiting BARBER) | CONFIRMED | confirm_booking | barber/admin | `_occupy` again (`SlotTakenError` if taken) [B5]; reminders |
| PENDING(awaiting=BARBER) | REJECTED | reject_booking | barber/admin | `_release`; cancel notifications |
| PENDING(awaiting=BARBER) | PENDING(awaiting=CLIENT) | propose_time | barber/admin | `proposal_count < 2`, else `ProposalLimitError`; `original_start_at` set on the first proposal; `_release` + `_occupy` at the new time (end/block_end recomputed); `expire_at = max(min(now + proposal_reply_min, new_start - cutoff), now + min_decision_min)`; cancel `rping` |
| PENDING(awaiting=CLIENT) | CONFIRMED | answer_proposal(accept=True) | client | `decided_at`; reminders |
| PENDING(awaiting=CLIENT) | CANCELLED_BY_CLIENT | answer_proposal(accept=False) | client | reason `proposal_declined`; never late; `_release` |
| PENDING (either) | EXPIRED | expire_due_pending | system | `_release`; cancel notifications; `cancelled_by=SYSTEM` |
| PENDING, CONFIRMED | CANCELLED_BY_CLIENT | cancel_by_client | client | `start_at > now` else `TooLateError`; **late only if CONFIRMED** and `start_at - now < free_cancel_hours` → `late_cancel=True`, `late_cancel_count += 1` [D6]; `_release` |
| PENDING, CONFIRMED | CANCELLED_BY_BARBER | cancel_by_staff, add_time_off, deactivate_barber, set_blocked | barber/admin | reason required; `_release`; cancel notifications |
| CONFIRMED | COMPLETED | complete_booking (allowed from `start_at - 30 min`), auto_complete_due | barber/admin, system | `_release`; `completed_at`; bonus (below); schedule `ret`, `rev`, and `mcf` for manual bookings |
| CONFIRMED | NO_SHOW | mark_no_show | barber/admin | `now >= start_at + 15 min` else `TooEarlyError`; `_release`; `no_show_count += 1` |
| COMPLETED (auto_completed, `now <= completed_at + noshow_flip_window_hours`) | NO_SHOW | mark_no_show | barber/admin | `bonus.reverse_for_booking`; `no_show_count += 1`; cancel `ret`/`rev`. Past the window: `TooLateError` [#30] |

Permissions:
- Barber actions: the actor is the linked user of `booking.barber_id`, or an admin.
- Client actions: `booking.client_id == client_user_id`.

**Completion and bonus.** `complete_booking` and `auto_complete_due` share `_complete(session, booking, auto: bool, now)`:
1. Set the status and `_release`.
2. If `bonus_cost > 0`, try `bonus.apply_redeem`. If the balance is short, complete anyway with `final_price = price_snapshot`, `bonus_redeemed=False` and `bonus_failed=True` [A5]. Completion is never blocked.
3. If `source == MANUAL`, earning waits: `needs_visit_confirm=True` and `mcf` is scheduled (only when `client_id` is set). Otherwise call `bonus.award_for_completion`.
4. Return `CompleteResult`.

`confirm_manual_visit(yes=True)` awards through `bonus.award_for_completion`. `yes=False` writes the audit entry `manual_visit_denied`, and the bot alerts the admins [D2]. Every manual completion is audited as `booking.manual_complete`.

`auto_complete_due` selects CONFIRMED bookings with `end_at + auto_complete_after_hours <= now` and completes them with `auto=True`. The nightly digest at 23:00 asks the barber first.

### 4.12 `app/services/bonus.py`
```python
async def get_bonus_status(session: AsyncSession, user_id: int, *, recent_limit: int = 5) -> BonusStatus
    # reserved = SUM(bonus_cost) over user's ACTIVE bookings; available = balance - reserved
async def quote_redeem(session: AsyncSession, *, user_id: int, price_uzs: int) -> RedeemQuote
    # stamps: possible = enabled and available >= required; cost = required;
    #         final = 0 if reward_type="free" else price*(100-value)//100
    # cashback: cost = min(available, price*max_redeem_percent//100); possible = cost > 0; final = price - cost
async def award_for_completion(session: AsyncSession, *, booking_id: int) -> tuple[int, int, int | None]   # internal
    # returns (earned, welcome_credited, referral_credited_to)
    # earn: client_id set, loyalty_enabled, not bonus_redeemed (stamps mode), and no earn row for
    #   (user, earn_day = local day of start_at) → stamps: +1; cashback: +final_price*cashback_percent//100 (skip if 0)
    # welcome [#45]: first COMPLETED booking of the user and welcome_bonus > 0 → +welcome_bonus, reason WELCOME
    # referral [#46][P2]: first COMPLETED, not auto_completed, referrer exists, not blocked, not deleted,
    #   referrer.phone != user.phone, referral_enabled → referrer +referral_bonus, reason REFERRAL, booking_id = this
async def apply_redeem(session: AsyncSession, *, booking_id: int) -> bool       # internal; -bonus_cost REDEEM; False if balance short
async def reverse_for_booking(session: AsyncSession, *, booking_id: int) -> int  # internal
    # REVERSAL rows negating EARN/WELCOME/REFERRAL rows of this booking and restoring REDEEM; clamps so that
    # no balance goes below 0; returns total reversed for the client
async def adjust_bonus(session: AsyncSession, *, actor_user_id: int, user_id: int, delta: int, comment: str) -> BonusStatus
    # admin only; comment required (InputError(field="comment")); balance may not go < 0; audit [#48]
async def recompute_balance(session: AsyncSession, user_id: int) -> int
```
Every ledger write updates `users.bonus_balance` with an atomic `UPDATE … SET bonus_balance = bonus_balance + :d`. The rows carry `unit = current loyalty_mode`.

### 4.13 `app/services/notifications.py`
```python
async def schedule_for_booking(session: AsyncSession, *, booking_id: int, now: datetime | None = None) -> None   # internal
    # PENDING(awaiting BARBER), created inside working hours: rping at created + pending_reping_min if < expire_at - 5m
    # CONFIRMED: r24 at start-24h if enabled and start - now > 24h; r2 at start-2h if enabled and start - now > 2h;
    #            cancels rping
    # COMPLETED: ret at clamp_to_daytime(start + return_reminder_weeks) if weeks > 0 and client_id;
    #            rev at clamp_to_daytime(end + review_delay_hours) if reviews_enabled, client_id, not auto_completed;
    #            mcf at now if source=MANUAL and client_id
    # upsert by (booking_id, kind); never reschedules a row that has claimed_at set
async def cancel_for_booking(session: AsyncSession, *, booking_id: int, kinds: list[NotificationKind] | None = None,
                             now: datetime | None = None) -> None      # internal; unclaimed rows only
async def claim_due(session: AsyncSession, *, limit: int = 50, now: datetime | None = None) -> list[DueNotification]  # internal
    # rows with send_at <= now, claimed_at/sent_at/cancelled_at NULL. In the same tx:
    #   cancel (cancelled_at=now) rows that are STALE or INVALID, set claimed_at=now on the rest, return them.
    # STALE [C5]: r24 now > send_at+3h; r2 now > send_at+30m; rping now > booking.expire_at-5m;
    #             ret now > send_at+3d; rev now > send_at+24h; mcf now > send_at+24h
    # INVALID: r24/r2 need CONFIRMED and start_at > now (r2 also client_ack_at IS NULL);
    #          rping needs PENDING awaiting BARBER; ret needs no newer COMPLETED and no ACTIVE booking of client;
    #          rev needs no review; mcf needs visit_confirmed IS NULL; client-facing kinds need client not deleted
async def mark_sent(session: AsyncSession, *, notification_id: int, now: datetime | None = None) -> None   # internal
async def mark_failed(session: AsyncSession, *, notification_id: int, error: str) -> None                  # internal
    # attempts += 1, last_error; the row stays claimed (at most once) [A4]
```

### 4.14 `reviews.py` [P2], `fsm_store.py`, `stats.py`, `audit.py`
```python
# reviews.py
async def submit_rating(session: AsyncSession, *, booking_id: int, client_user_id: int, rating: int) -> ReviewResult
    # booking COMPLETED and client owns it; 1..5; one per booking (repeat → returns existing, idempotent)

# fsm_store.py   (used only by app/bot/fsm_storage.py; each call runs in its own short tx opened by the storage)
async def get_record(session: AsyncSession, key: str) -> tuple[str | None, dict]
async def set_state(session: AsyncSession, key: str, state: str | None) -> None
async def set_data(session: AsyncSession, key: str, data: dict) -> None
async def purge_older_than(session: AsyncSession, *, days: int = 7, now: datetime | None = None) -> int

# stats.py
async def get_stats(session: AsyncSession, *, actor_user_id: int, start: date, end: date,
                    barber_id: int | None = None) -> StatsDTO
    # admin only; buckets by local day via day_bounds_utc; revenue = SUM(final_price_uzs) of COMPLETED;
    # manual_completed share shown to flag stamp farming [D2]

# audit.py
async def write_audit(session: AsyncSession, *, actor_user_id: int | None, action: str, entity: str,
                      entity_id: int | None, payload: dict | None = None) -> None
```

### 4.15 `app/ops/backup.py` [#93][G5]
```python
def backup_sqlite(db_path: Path, backup_dir: Path, *, keep: int = 14, now: datetime | None = None) -> Path
    # sqlite3 online backup API (src.backup(dst)) → bot-YYYY-MM-DD.db, then PRAGMA integrity_check on the copy;
    # deletes oldest beyond keep; returns path. CLI prints the restore command (stop bot; cp backup data/bot.db).
```
The tick runs it daily at `backup_time` in `asyncio.to_thread`, outside any transaction, and then sends the file to the env admins as a document if it is under 45 MB.

---

## 5. Config and runtime

### 5.1 `app/config.py`
```python
CONSENT_VERSION: int = 1          # bump when the privacy text changes → users re-asked [#73]
PROJECT_ROOT: Path                # Path(__file__).resolve().parents[1]

class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")
    bot_token: SecretStr | None = None                   # BOT_TOKEN (required by main.py only, so tests import freely) [C3]
    database_url: str = "sqlite+aiosqlite:///data/bot.db" # DATABASE_URL; relative → PROJECT_ROOT [G4]
    data_dir: Path = Path("data")                        # DATA_DIR; relative → PROJECT_ROOT
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")
    shop_tz: str = "Asia/Tashkent"                       # SHOP_TZ — never TZ [C1]
    default_lang: str = "uz"                             # DEFAULT_LANG
    log_level: str = "INFO"
    redis_url: str | None = None                         # REDIS_URL → RedisStorage; else DbStorage
    scheduler_tick_seconds: int = 30
    throttle_per_minute: int = 30                        # callbacks + messages per user
    db_echo: bool = False
    @property
    def admin_ids(self) -> frozenset[int]: ...
@lru_cache
def get_config() -> Config: ...
```
`.env.example` lists `BOT_TOKEN=`, `ADMIN_IDS=`, `DATABASE_URL=sqlite+aiosqlite:///data/bot.db`, `SHOP_TZ=Asia/Tashkent`, `DEFAULT_LANG=uz`, `LOG_LEVEL=INFO`, `#REDIS_URL=`. Business values live in the settings table (§4.4).

### 5.2 Startup (`app/bot/main.py`)
1. Load `cfg`. Fail if `bot_token` is missing. Set up logging, then call `clock.assert_tz_on_grid()` and log the resolved zone and the current local time [C1].
2. Take the `fcntl.flock` on `data_dir/bot.lock`, or exit with code 3 [A4].
3. `engine = create_engine()`, `await assert_schema_current(engine)` [#94], `sm = create_sessionmaker(engine)`.
4. In `write_tx`, run `token = settings.ensure_setup_token()`. If a token comes back, log at WARNING: `No admin. Open https://t.me/<bot_username>?start=setup_<token>` [G1].
5. Create `bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))`. Storage is `RedisStorage` if `redis_url` is set, otherwise `DbStorage(sm)` [E3].
6. Create `dp = Dispatcher(storage=...)` and set `dp["sm"] = sm` and `dp["notifier"] = Notifier(bot, sm)`.
7. Register the outer update middlewares in this order: `PrivateOnlyMiddleware` → `ThrottleMiddleware` → `DebounceMiddleware` → `UserMiddleware`.
8. `dp.include_router(build_root_router())` and `dp.errors.register(errors.on_error)`.
9. Call `set_my_commands` for `uz` (default scope and `language_code="uz"`) and `ru`. Barbers and admins get extra commands through `BotCommandScopeChat`, refreshed on link and promotion [#88].
10. `scheduler.start(bot, sm, notifier)`, then `await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)`. On shutdown: `scheduler.shutdown(wait=False)`, `await bot.session.close()`, `await engine.dispose()`, release the lock.

### 5.3 Middlewares
- **private.py** [#90][E7]: drops every update whose chat is not private. On `my_chat_member` in a group, the bot calls `leave_chat`.
- **throttle.py** [H3]: one TTL bucket per user, covering messages and callbacks. Sliding window of 60 s, limit `throttle_per_minute`, entries expire after 10 minutes of inactivity, at most 10 000 entries. Over the limit: a callback is answered with `t(lang,"common.too_fast")`, a message is silently dropped.
- **debounce.py** [#10]: a second callback with the same `(user_id, data)` within 1.0 s is answered with an empty `call.answer()` and dropped.
- **user.py**:
  1. In `read_tx`, run `ctx = users.load_context(tg_id=...)`.
  2. If `ctx` is None or `needs_sync(...)` is true, or the update is `/start` with a payload, run `ctx = users.ensure_user(...)` in `write_tx` [A2].
  3. If `ctx.roles.is_barber` and `ctx.barber_reachable is False`, run `barbers.mark_reachable_by_user` in `write_tx` [B4].
  4. Inject `data["user"]`, `data["roles"]` and `data["lang"]`.
  5. If the user is blocked, answer `t(lang,"common.blocked")` once per 10 minutes and drop the update.
  6. **Consent gate:** if `user.consent_version` is below `CONSENT_VERSION`, only these may pass: `/start`, `/whoami`, `/delete_me`, `LangCb`, `ConsentCb`. Everything else re-shows the onboarding screen.
  7. Exit commands are allowed through from any FSM state: the main-menu reply buttons, `/start`, `/menu` and `/cancel` [E1].

### 5.4 Mutating-handler pattern (mandatory)
```python
@router.callback_query(SlotConfirm...)
async def on_confirm(call: CallbackQuery, state: FSMContext, sm, notifier, user, lang):
    data = await state.get_data()
    try:
        async with write_tx(sm) as s:
            res = await bookings.create_booking(s, client_user_id=user.id, barber_id=data["barber_id"], ...)
    except SlotTakenError as e:
        await ui.slot_taken_refresh(call, state, lang)        # alert + redraw the slot grid for the same day
        return
    except ServiceError as e:
        await call.answer(t(lang, f"err.{e.code}", **e.ctx), show_alert=True)
        return
    await state.clear()
    await screen.render(call, state, t(lang, "book.sent" if not res.auto_confirmed else "book.confirmed", ...))
    await notifier.booking_created(res)                       # network I/O only after commit
```
`write_tx` rolls back on the `ServiceError`, so nothing partial is committed [A1].

---

## 6. Bot layer details

### 6.1 Client UX
**Onboarding [#87][#73]:** `/start` for a new user goes through these steps:
1. Language picker: "🇺🇿 O'zbekcha" / "🇷🇺 Русский". It is skipped when `language_code` is uz/ru, but the hub has a "🌐 Til" button to change it.
2. A one-screen privacy notice with "✅ Roziman" (I agree), which calls `accept_consent`.
3. The hub.

The phone is requested only at the first booking.

**Reply keyboard (client):**
```
[✂️ Navbatga yozilish]
[📋 Mening navbatlarim] [🎁 Bonuslarim]
[💈 Ustalar va narxlar] [📍 Manzil va aloqa]
```
**Hub (inline, edited in place [#7]):** the greeting text, plus "🔁 Avvalgidek yozilish (Akmal, Soch + soqol)" when `get_last_completed` returns a hint [#15], "✂️ Navbatga yozilish", "📋 Mening navbatlarim", "🎁 Bonuslarim", "👥 Do'stni taklif qilish" [P2], and "🌐 Til".

**Booking wizard (BookingFlow, one message, `nav` back stack):**
1. **Service:** "Xizmatni tanlang:" with `Soch olish — 60 000 so'm · 30 daq` buttons (one per row) and ❌. It is skipped when the shop has no services, or when the client entered from a barber card and that barber offers exactly one service.
2. **Barber:** "Sartaroshni tanlang:". First "🤝 Farqi yo'q" (any barber [#1]), then one button per public barber offering the service, then ⬅️ and ❌. The "ℹ️" button opens the barber card (photo, bio [#57]) through `screen.render`, with "✂️ Shu ustaga yozilish" and ⬅️. Skipped when the barber is preset.
3. **Day [#4]:** `Bugun`, `Ertaga`, `Indinga`, then `Pa 29.10`-style labels up to the horizon (at most 7 quick buttons, 3 per row). A day without slots shows as `Ya 01.11 · band` and sends `noop`, which answers "Bu kunda bo'sh vaqt yo'q". "Bugun" is hidden when it has no slots. "📅 Boshqa kun" opens the month calendar [#3][P2] when the horizon is over 7 days.
4. **Slot [#5]:** "Bo'sh vaqtlar — 26-oktabr, dushanba:". Group header rows (`noop`) "🌅 Ertalab" (before 12:00), "☀️ Kunduzi" (12:00 up to 17:00) and "🌙 Kechqurun" (17:00 and later), with 4 time buttons per row. Empty groups are omitted. An empty day shows "Bo'sh vaqt yo'q" with ⬅️. The last row is ⬅️ ❌.
5. **Comment [#11]:** "Izoh qoldirasizmi? (masalan: soqol ham)" with "➡️ Izohsiz davom etish", ⬅️ and ❌. Free text up to 300 characters.
6. **Contact** (only if the user has no verified phone [D1]):
   - The wizard message is edited to "📱 Navbat uchun raqamingizni yuboring" with ❌.
   - A separate prompt message is sent with a one-time reply keyboard `[📱 Raqamni yuborish]`.
   - Only `F.contact` with `contact.user_id == from_user.id` is accepted. Anything else gets `t("contact.only_own")`. A typed number gets `t("contact.use_button")`.
   - After a valid contact: the bot deletes the prompt (best effort), strips the old wizard message's keyboard, sends "✅ Raqam saqlandi" with the main reply keyboard restored, and sends the **summary as a new message**. That new message's id becomes the new `msg_id` [E2].
7. **Summary / confirm:**
   - The card shows: barber (or "Farqi yo'q"), service, `26-oktabr, dushanba, 10:30`, price, name, phone and comment.
   - Buttons: "✅ Tasdiqlash", "✏️ Ismni o'zgartirish" (opens `name_input`), "🎁 Bonusdan foydalanish" or "🎁 Bonussiz" (toggle, shown only when the quote is possible, with the discounted price shown), ⬅️ and ❌.
   - On confirm, the handler follows §5.4.

The wizard's callback handlers check `call.message.message_id == data["msg_id"]` and answer STALE otherwise [E2][#10]. If the FSM data is missing, they answer `t("common.session_expired")` and render the hub.

**Rebook paths:** "🔁 Avvalgidek", `RebookCb` (from completed/rejected/expired/cancel notifications) and the barber card all start BookingFlow with the barber and service preset, at the day step. If the barber is no longer public, the flow starts at the barber step.

**My bookings:** lists upcoming PENDING and CONFIRMED bookings and the last 5 past ones. Each item can have:
- "❌ Bekor qilish" on a CONFIRMED booking: a confirm dialog, which warns when the cancel will be late.
- "↩️ So'rovni qaytarib olish" on a PENDING booking awaiting the barber [#17]: no confirmation needed.
- "✅ Qabul qilish / ❌ To'g'ri kelmaydi" on a PENDING booking awaiting the client [#21].

**Info:**
- "💈 Ustalar va narxlar" shows the price list [#58] and the barber buttons, which open the cards.
- "📍 Manzil va aloqa" shows the address text, the phone, and weekly hours from `get_shop_week_hours`, with "Bugun: 10:00–20:00 (ochiq)" [#60]. Then `send_venue` only if lat and lon are set, plus URL buttons for each non-empty map link (Yandex, Google, 2GIS) [#59][E8].
- "🎁 Bonuslarim":
  - Stamps mode: `✂️✂️✂️▫️▫️ 3/5`, then "Yana 2 ta tashrifdan keyin 6-chi soch olish bepul" [#43].
  - Cashback mode: the balance in so'm [P2].
  - Both: the recent ledger, plus the referral link `https://t.me/<bot>?start=ref_<code>` with a share button [P2][#46].

**Reminders:**
- `r24` has "✅ Kelaman" and "❌ Bekor qilish". `r2` is plain information.
- `ret`: "Soch olish vaqti keldi ✂️" with "🔁 Avvalgidek yozilish" [#37].
- `rev` [P2]: ⭐1 to ⭐5 buttons. A rating of 3 or less is forwarded to the admins.
- `mcf`: "Bugun soat 15:00 da Ali'da edingizmi?" with [Ha] and [Yo'q].

### 6.2 Barber UX
**Reply keyboard (barber):**
```
[📅 Bugungi navbat] [🗓 Kelgusi kunlar]
[⏸ Hozir bandman] [⛔ Vaqtni band qilish]
[➕ Qo'lda yozish] [👤 Mijoz menyusi]
```
**Decision message** (sent to the barber, and to admins when they are fallback targets):
- Content: `🆕 Yangi so'rov`, then client name, phone as plain text (Telegram makes it tappable [E8]), service, `26-oktabr, dushanba, 10:30`, "Tashriflar: 4 · Kelmagan: 1" (visits and no-shows), "🆕 Yangi mijoz" when the client is new, and the comment.
- Buttons: `[✅ Qabul qilish] [❌ Rad etish]` / `[🕒 Boshqa vaqt taklif qilish]`.
- After any decision, `notifier` edits **every** stored decision message to the outcome without buttons, e.g. "✅ Qabul qilindi (Ali)", "⌛ Muddati o'tdi" (expired) or "❌ Mijoz bekor qildi" (client cancelled).

**Reject:** the reason buttons are `Band`, `Kasal`, `Boshqa…` (free text through `RejectFlow`) and `Sababsiz`.

**Propose [#21]:** the bot lists `list_proposal_slots` as `ProposeCb` buttons (day label plus time), then calls `propose_time`. The client gets "Ali 26-oktabr 11:00 ni taklif qilmoqda" with `[✅ Qabul qilish] [❌ To'g'ri kelmaydi]`.

**Today / upcoming [#25]:** one line per booking: time, name, phone, service and a status icon. Each booking has a button that opens its card, where the actions are ✅ Bajarildi (done), 🚫 Kelmadi (no-show, offered once `start + 15 min` has passed), and ❌ Bekor qilish (staff cancel, with a reason). An auto-completed booking within the flip window shows "🚫 Aslida kelmadi" (didn't actually come).

**Busy now [F1]:** `[30 daq] [60 daq]` calls `busy_now`. If the block overlaps a booking, the bot warns: "⚠️ 15:30 dagi Aziz bilan kesishadi". Nothing is cancelled.

**Block time:** day → "Butun kun" (whole day) or a start and end time chosen from a 30-minute grid → reason → conflict preview with "Bekor qilib xabar berish" (cancel and notify) or "Saqlash, navbatlar qolsin" (save, keep the bookings) [#27][#31].

**Manual booking [#26]:** service → day → slot (lead time ignored) → name → phone (typed, optional, "O'tkazib yuborish" to skip) → confirm. The bot shows whether the booking is linked to a client: "🔗 Bot mijoziga bog'landi" or "Bog'lanmagan".

**Morning summary** (at `daily_summary_time`): today's list. Every PENDING booking awaiting the barber is sent as its own decision message with buttons [B1]. Admins get a short digest of PENDING bookings whose barber is unreachable.

**Nightly digest** (at `nightly_check_time`): unresolved CONFIRMED bookings with Done and No-show buttons, plus "Belgilanmasa, N soatdan keyin avtomatik bajarildi deb yopiladi" (if not marked, they auto-close as done after N hours) [#30].

### 6.3 Admin UX
**Reply keyboard (admin):**
```
[💈 Sartaroshlar] [🧾 Xizmatlar]
[🕒 Ish vaqti] [🌴 Dam olish kunlari]
[📋 Navbatlar] [👥 Mijozlar]
[📊 Statistika] [⚙️ Sozlamalar]
[🛡 Adminlar] [🔄 Rol]
```
Sections map to `AdminCb.s` (§7):
- Barbers: create, edit fields, services, step and buffer, auto-accept, invite link (with expiry), link approvals, unlink, deactivate (preview, then confirm cancelling the future bookings), activate. Badges "🔴 bog'lanmagan" (unlinked) and "⚠️ yetib bo'lmaydi" (unreachable).
- Services: create, edit, activate and deactivate.
- Hours: per barber and weekday, with a break, "Hamma kunlarga nusxa" (copy to all days), and the out-of-hours warning.
- Time off: per barber or shop-wide, with the conflict flow.
- Bookings: filters by day, barber and status, a phone/name search, pages of 10, and cancel.
- Clients: phone search (audited), profile, bonus ± with a comment, block and unblock (cancels their bookings), reset no-shows.
- Settings: grouped by §4.4 categories, the location share, and the loyalty mode.
- Stats: today, 7 days, this month, or a custom range, per barber, with top services and the manual share.
- Admins: list, promote by forwarded message or tg id, demote promoted admins.

**Link approval [D4]:** each admin gets "Aziz (@aziz, id 123) Ali sifatida bog'lanmoqchi" (wants to link as Ali) with `[✅ Tasdiqlash] [❌ Rad etish]`.

### 6.4 `notifier.py`
```python
class Notifier:
    def __init__(self, bot: Bot, sm: async_sessionmaker[AsyncSession]) -> None
    async def booking_created(self, res: BookingCreated) -> None
        # client "book.sent"/"book.confirmed"; decision msg to each target → add_decision_message (own write_tx)
    async def booking_confirmed(self, b: BookingDTO) -> None      # client: confirmed + address/venue; edit decision msgs
    async def booking_rejected(self, b: BookingDTO) -> None       # client: apology + reason + RebookCb
    async def booking_proposed(self, r: ProposalResult) -> None   # client: proposal buttons; edit decision msgs
    async def proposal_answered(self, b: BookingDTO) -> None      # barber (+admin targets): accepted/declined
    async def booking_expired(self, b: BookingDTO) -> None        # client (rebook), edit decision msgs, admins
    async def booking_cancelled(self, b: BookingDTO, *, by_client: bool) -> None
    async def booking_completed(self, r: CompleteResult) -> None  # client thanks + balance (bot bookings only)
    async def booking_no_show(self, r: NoShowResult) -> None      # client polite note
    async def time_off_cancellations(self, items: list[BookingDTO]) -> None   # each client: apology + RebookCb [#31]
    async def due_notification(self, n: DueNotification) -> bool  # True when delivered
    async def morning_summary(self, day: date) -> None
    async def nightly_digest(self) -> None
    async def link_requested(self, req: LinkRequest) -> None
    async def link_decided(self, barber: BarberDTO, user_tg_id: int, approved: bool) -> None
    async def low_rating(self, r: ReviewResult) -> None           # [P2]
    async def manual_visit_denied(self, b: BookingDTO) -> None
    async def alert_admins(self, key: str, **kw: object) -> None  # localized per admin
```
Rules:
- The notifier opens its own short `read_tx` or `write_tx` only to load data or record message ids. It never sends inside a transaction.
- On `TelegramForbiddenError` to a barber: `set_reachable(False)` in a `write_tx`, then alert the admins.
- On `TelegramForbiddenError` to a client: log it only.
- On `TelegramRetryAfter`: sleep `retry_after`, retry once.
- `TelegramBadRequest` "message is not modified" / "message to edit not found" is ignored.
- Global pacing: `asyncio.Semaphore(20)`, at most 25 messages per second.
- Every message text is localised with the **recipient's** `lang`.

### 6.5 `scheduler.py`
One `AsyncIOScheduler` interval job, `tick`, every `scheduler_tick_seconds`, with `max_instances=1` and `coalesce=True`. There are no cron jobs [C4]. Each step uses its own short transaction:
1. `write_tx`: `expired = expire_due_pending()`. After commit, `notifier.booking_expired` for each.
2. `write_tx`: `done = auto_complete_due()`. After commit, `notifier.booking_completed` for each.
3. `write_tx`: `due = claim_due(limit=50)`. After commit, for each: `ok = await notifier.due_notification(n)`, then `write_tx` → `mark_sent` or `mark_failed` [A2][A4].
4. Daily jobs, each guarded by a marker:
   - The job runs if local now is at or after `HH:MM` and `get_marker("_job.<name>") != str(local_today)`.
   - Order inside the job: `set_marker` in a `write_tx`, commit, then run. This makes the job at most once a day.
   - The morning summary is skipped (but marked) when local now is later than `daily_summary_time + 3h`.
   - The jobs are `morning`, `nightly` and `backup`.
5. Once per hour: `fsm_store.purge_older_than(7)`.

Settings changes take effect on the next tick without a restart.

### 6.6 `i18n.py` and locales
```python
def t(lang: str, key: str, **kw: object) -> str      # STRINGS[lang].get(key) or uz fallback; str.format(**kw)
def labels(key: str) -> set[str]                     # the key's text in all locales (reply-button matching)
def h(s: str | None) -> str                          # html.escape(s or "", quote=False) — wrap EVERY user value
def fmt_dt(dt: datetime, lang: str) -> str           # "26-oktabr, dushanba, 10:30" / "26 октября, понедельник, 10:30"
def fmt_day_btn(d: date, today: date, lang: str) -> str   # Bugun | Ertaga | Indinga | "Pa 29.10"
def fmt_time(dt: datetime) -> str                    # "10:30" local
def fmt_price(n: int, lang: str) -> str              # "60 000 so'm" / "60 000 сум"
def fmt_stamps(progress: int, required: int) -> str  # "✂️✂️✂️▫️▫️"
```
- **Uzbek months:** yanvar, fevral, mart, aprel, may, iyun, iyul, avgust, sentabr, oktabr, noyabr, dekabr.
- **Uzbek weekdays (Mon..Sun):** dushanba, seshanba, chorshanba, payshanba, juma, shanba, yakshanba.
- **Short weekday labels:** Du, Se, Ch, Pa, Ju, Sh, Ya.
- Calendar check [C7]: 2026-10-24 is a Saturday (shanba), 2026-10-25 a Sunday (yakshanba), and 2026-10-26 a Monday (dushanba). Tests derive expected labels from `date.weekday()`, never from literals in the specs.
- **Key namespaces:** `common.*`, `menu.*`, `hub.*`, `book.*`, `my.*`, `info.*`, `bonus.*`, `barber.*`, `admin.*`, `notify.*`, `err.*`, `settings.*`.
- `tests/test_i18n.py` checks:
  - `uz` and `ru` have identical key sets, and identical `{placeholders}` per key.
  - Every `ServiceError.code` has an `err.<code>` key.
  - Every reply label is unique across all keyboards.
- If `ru` is cut [P2], `ru.py` falls back to the uz text, but the key set stays complete.

### 6.7 `screen.py` (edit-in-place helper [#7][E4])
```python
async def render(target: CallbackQuery | Message, state: FSMContext, text: str,
                 kb: InlineKeyboardMarkup | None = None, *, photo: str | None = None,
                 new_message: bool = False) -> int
    # returns message_id and stores it as data["msg_id"].
    # Callback + same kind (text→text, photo→photo): edit_message_text / edit_message_media(+caption).
    # Kind change, Message target, new_message=True, or edit failure (not found / too old): delete the old
    # message (best effort) and send a new one.
    # Swallows "message is not modified"; always calls call.answer() once.
```

### 6.8 `handlers/errors.py`
`on_error(event: ErrorEvent)` does three things:
- Logs the exception with the update id.
- If the update is a callback, answers it with `t(lang,"err.error")` so the spinner never hangs.
- Calls `notifier.alert_admins("admin.crash", ...)`, rate-limited to 1 alert per 5 minutes per exception type [E4].

### 6.9 Commands
| Command | Who | Action |
|---|---|---|
| `/start [payload]` | all | Payloads: `inv_<token>` → request_link; `setup_<token>` → claim_setup_token; `ref_<code>`, `src_<tag>` (handled in ensure_user). Then the hub. |
| `/menu` | all | Clears the FSM state and posts a fresh hub with the reply keyboard. |
| `/cancel` | all | Clears the FSM state with "Bekor qilindi" (cancelled). |
| `/my` | client | My bookings. |
| `/today` | barber | Today's queue. |
| `/whoami` | all | Shows tg_id, roles and language [G1]. |
| `/lang` | all | Language picker. |
| `/delete_me` | all | Confirm dialog, then `users.delete_me` [H1]. |
| `/help` | all | Help text per role. |

---

## 7. Callback data (`app/bot/callbacks.py`, aiogram `CallbackData`, separator `:`)

Every packed string must be 64 bytes or less (`tests/test_callbacks.py` packs the worst case of each). Every handler re-validates on the server. A stale button gets `t("common.stale")` as an alert ("Bu tugma eskirgan").

| Class | prefix | fields | example | use |
|---|---|---|---|---|
| `HubCb` | `h` | `a:str` ∈ book,my,bonus,info,prices,rebook,ref,lang,home | `h:book` | hub buttons |
| `LangCb` | `lg` | `l:str` ∈ uz,ru | `lg:ru` | language |
| `ConsentCb` | `cs` | `a:str` ∈ ok | `cs:ok` | privacy accept |
| `SvcCb` | `sv` | `s:int` (0=none) | `sv:3` | wizard service |
| `BarberCb` | `bb` | `b:int` (0=any) | `bb:12` | wizard barber |
| `BarberCardCb` | `bc` | `a:str` ∈ view,book,back, `b:int` | `bc:book:12` | barber card |
| `DayCb` | `dy` | `d:str` YYYYMMDD | `dy:20261026` | wizard day |
| `CalCb` | `cal` | `a:str` ∈ m,d, `v:str` (YYYYMM or YYYYMMDD) | `cal:m:202611` | [P2] month calendar |
| `SlotCb` | `sl` | `b:int`, `s:int`, `d:str`, `t:str` HHMM | `sl:12:3:20261026:1030` | wizard slot; carries the full context [E3] |
| `WizCb` | `wz` | `a:str` ∈ back,cancel,skip_comment,confirm,bonus_on,bonus_off,edit_name | `wz:confirm` | wizard navigation |
| `BookingCb` | `bk` | `a:str`, `id:int` | `bk:acc:123` | booking actions (below) |
| `RejectCb` | `rj` | `id:int`, `r:str` ∈ busy,sick,other,none | `rj:123:sick` | reject reason |
| `ProposeCb` | `pp` | `id:int`, `d:str`, `t:str` | `pp:123:20261026:1100` | propose a time |
| `RebookCb` | `rb` | `b:int`, `s:int` (0=none) | `rb:12:3` | rebook preset |
| `RateCb` | `rt` | `id:int`, `v:int` 1..5 | `rt:123:5` | [P2] rating |
| `QueueDayCb` | `qd` | `d:str` | `qd:20261027` | barber day list |
| `BusyCb` | `bz` | `m:int` ∈ 30,60 | `bz:30` | busy now |
| `BlockCb` | `bl` | `a:str` ∈ day,whole,start,end,cancelcf,keep,abort, `d:str`, `t:str` | `bl:start:20261026:1400` | block time |
| `ManualCb` | `mn` | `a:str` ∈ svc,day,slot,skipphone,confirm,abort, `s:int`, `d:str`, `t:str` | `mn:slot:3:20261026:1500` | manual booking |
| `LinkCb` | `lk` | `a:str` ∈ ok,no, `b:int`, `u:int` | `lk:ok:5:42` | barber link approval |
| `AdminCb` | `ad` | `s:str`, `a:str`, `id:int=0`, `v:str=""` | `ad:br:edit:5:bio` | admin sections |
| `RoleCb` | `rl` | `r:str` ∈ c,b,a | `rl:b` | role switch |
| `PageCb` | `pg` | `k:str`, `p:int`, `f:str=""` | `pg:adbk:2:20261026` | pagination |
| — | `noop` | — | `noop` | headers, disabled days |

`BookingCb.a`:

| `a` | meaning | who | service |
|---|---|---|---|
| `acc` | accept | barber/admin | confirm_booking |
| `rej` | reject → reason picker | barber/admin | reject_booking |
| `prop` | open the proposal slot list | barber/admin | list_proposal_slots |
| `card` | open the booking card | barber/admin | get_booking_for_actor |
| `done` | mark done | barber/admin | complete_booking |
| `ns` | no-show (also flips an auto-completed booking) | barber/admin | mark_no_show |
| `scx` | staff cancel → reason | barber/admin | cancel_by_staff |
| `ccx` | client cancel → confirm dialog | client | — |
| `ccy` | client cancel confirmed | client | cancel_by_client |
| `wd` | withdraw a pending request | client | cancel_by_client |
| `pya` / `pno` | accept / decline a proposal | client | answer_proposal |
| `ack` | "Kelaman" | client | client_ack |
| `vy` / `vn` | manual visit yes / no | client | confirm_manual_visit |

`AdminCb.s`: `br` barbers, `sv` services, `wh` hours, `to` time off, `bk` bookings, `cl` clients, `st` settings, `stat` stats, `adm` admins. The verbs in `a` are `list`, `new`, `edit`, `tgl`, `inv`, `unl`, `deact`, `act`, `wd`, `copy`, `pick`, `set`, `del`, `prev`, `ok`. `v` holds a small value: a field name, a weekday, or a setting key.

---

## 8. FSM (`app/bot/states.py`)

```python
class Onboarding(StatesGroup):        lang = State(); consent = State()
class BookingFlow(StatesGroup):
    service = State(); barber = State(); barber_card = State(); day = State(); calendar = State()
    slot = State(); comment = State(); contact = State(); name_input = State(); confirm = State()
class RejectFlow(StatesGroup):        reason_text = State()                     # data: booking_id
class StaffCancelFlow(StatesGroup):   reason_text = State()                     # data: booking_id
class BlockTimeFlow(StatesGroup):     day = State(); start = State(); end = State(); reason = State(); confirm_conflicts = State()
class ManualBookingFlow(StatesGroup): service = State(); day = State(); slot = State(); name = State(); phone = State(); confirm = State()
class AdminBarberFlow(StatesGroup):   name = State(); bio = State(); photo = State(); edit_field = State(); deact_reason = State()
class AdminServiceFlow(StatesGroup):  name = State(); duration = State(); price = State(); edit_field = State()
class AdminHoursFlow(StatesGroup):    weekday = State(); start = State(); end = State(); break_ = State()
class AdminTimeOffFlow(StatesGroup):  target = State(); start_day = State(); end_day = State(); hours = State(); reason = State(); confirm_conflicts = State()
class AdminSettingsFlow(StatesGroup): value = State()                           # data: key
class AdminClientFlow(StatesGroup):   search = State(); bonus_delta = State(); bonus_comment = State()
class AdminBookingsFlow(StatesGroup): search = State()
class AdminAdminsFlow(StatesGroup):   promote = State()
```
BookingFlow data keys (written by BOT only):
- `msg_id: int`
- `nav: list[str]` (the stack of state names, for ⬅️) [#8]
- `entry: str` (service_first | barber_first | rebook)
- `service_id: int | None`
- `barber_id: int | None` (None = any)
- `day: str` (ISO date)
- `start_at: str` (ISO UTC)
- `comment: str | None`
- `use_bonus: bool`

Rules:
- ⬅️ pops `nav` and redraws the previous step from the stored data. Earlier choices are kept.
- Every text-input state shows an inline "❌ Bekor qilish" button [E1][#9].
- The main-menu reply buttons and `/start`, `/menu`, `/cancel` are registered in `onboarding.py` with `StateFilter("*")`. That router is included before every FSM text handler, so these exits always win [E1].
- Wizard callbacks check `msg_id` (§6.1).

---

## 9. Router order and guards

`build_root_router()` includes the routers in this order. Every router carries `F.chat.type == "private"`, which is redundant with the middleware.
1. `onboarding` (exits with `StateFilter("*")`, `/start`, consent, language)
2. `barber_decide`
3. `barber_queue`
4. `barber_block`
5. `barber_manual`
6. `admin_admins`
7. `admin_barbers`
8. `admin_services`
9. `admin_hours`
10. `admin_timeoff`
11. `admin_bookings`
12. `admin_clients`
13. `admin_settings`
14. `admin_stats`
15. `client_booking`
16. `client_my`
17. `client_info`
18. `client_feedback`
19. `fallback` (last): an unknown message re-shows the hub, and an unknown callback answers STALE.

The barber routers filter on `IsBarber()` and the admin routers on `IsAdmin()`. Both filters read `data["roles"]`. Services check permissions again on their own. Admin decisions on bookings (`bk:acc` and others from fallback messages) are handled in `barber_decide`, whose filter is `IsBarber() | IsAdmin()`.

---

## 10. Tests (QA): the minimum

**Common setup:**
- `conftest.py` sets `os.environ.setdefault("BOT_TOKEN","0:test")` and `SHOP_TZ="Asia/Tashkent"` **before** any `app` import [C3].
- An in-memory engine with the same listeners, plus a file DB in `tmp_path` for race tests.
- `NOW = 2026-10-26 09:00 Asia/Tashkent`, which is a Monday.
- Barber Ali (linked, public, `auto_accept` per test) works Mon–Sat 10:00–20:00 with a 13:00–14:00 break, step 30, buffer 0.
- Services: 30 min at 60 000 so'm, and 60 min.
- `factories.py` builds everything through the services.

| File | Must cover |
|---|---|
| test_validation | `+998 90 123-45-67`, `998901234567`, `901234567` and `(90) 123 45 67` all give `+998901234567`; `12345`, `+7…` and letters are rejected; names `O'tkir` and `Gʻayrat` pass; `A`, 51 chars and `<b>x</b>` fail; comment over 300 chars fails; `mask_phone`. |
| test_clock | `day_bounds_utc`; grid floor/ceil; `assert_tz_on_grid` raises for `Asia/Kathmandu`; `clamp_to_daytime`; weekday labels derived from the calendar [C7]. |
| test_slots_pure | Break excluded; time off excluded; 60 min needs two steps; 60 min at 12:30 is not offered (crosses the break) and 19:30 is not offered (runs past close); lead time (`ceil_to_grid`); horizon; day off; shop-wide holiday; half-open edge (a booking ending 10:30 leaves 10:30 free); buffer 15 blocks the next 15 minutes; an end-of-block candidate (a manual booking 10:15–10:45 makes 10:45 offered) [F4]. |
| test_slots_db | Wrappers; "any barber" union; `get_available_days` with `barber_id=None`; unlinked barber gives no slots. |
| test_booking_create | Happy path; `PhoneRequiredError` without a verified phone; consent required; auto-accept vs new-client PENDING vs soft-block PENDING; "any barber" picks the least-loaded barber and falls through to the next on SlotTaken; snapshots incl. `block_end_at`. |
| test_booking_race | File DB, 2 sessions, `asyncio.gather`, 20 iterations. Same slot: exactly one wins and the other gets `SlotTakenError`. Overlap 10:00/60 against 10:30/30. **After the loser: `COUNT(bookings)` and `COUNT(booking_slots)` are unchanged** [A1]. |
| test_uow | `write_tx` rolls back on a `ServiceError` raised after a flush; `read_tx` leaves no writes; the AST check that there is no Telegram I/O inside tx blocks under `app/bot` [A2]. |
| test_booking_state_machine | Every allowed transition (parametrised), every forbidden one, idempotent repeats, cell count after every transition, the slot back in `get_free_slots`, late accept within grace [B5], and outside grace gives `TooLateError`. |
| test_proposals | propose → awaiting client, cells moved; accept → CONFIRMED; decline → CANCELLED_BY_CLIENT not late; a 3rd proposal gives `ProposalLimitError`; client reply expiry. |
| test_booking_limits | `max_active`=3 counted per user **and per phone**; `max_per_day`; overlap across barbers refused; adjacent same-barber bookings allowed [E5]; blocked user refused. |
| test_pending_expiry | Settings 60/10/15, barber 10–20. (a) Created Mon 11:00 for 15:00 expires 12:00. (b) Created Sun 23:00 for Mon 10:00 expires 09:50 [B1]. (c) Created Sun 23:00 for Mon 12:00 expires 11:00. (d) Created Mon 09:00 for 10:30 expires 10:20. (e) Created 14:25 for 15:00 expires 14:50 [B2]. `update_setting` rejects `min_lead_min=20` with cutoff 10 and decision 15. `expire_due_pending`. |
| test_cancel_policy | Free cancel more than 2 h ahead; a late cancel of CONFIRMED is flagged; withdrawing a PENDING booking inside 2 h is **not** late [D6]; a past booking gives `TooLateError`. |
| test_no_show_softblock | `TooEarlyError` before start+15; 3 no-shows mean soft-blocked and auto-accept skipped; `reset_no_shows`; auto-completed flip within 24 h reverses the bonus, and after 24 h gives `TooLateError`. |
| test_bonus | 5 completions on 5 different days give balance 5; a 6th with `use_bonus` has final 0 and completion leaves 0 with no stamp; two completions on the same local day earn once [D2]; no-show earns nothing; a stale double complete earns once; `bonus_cost` snapshot and the required stamps raised to 10 still complete without the bonus (`bonus_failed`) [A5]; reserved/available; welcome on the first visit only; [P2] referral credit only on the friend's first non-auto visit and never self; cashback quote and earn [P2]; manual adjust needs a comment and the balance stays ≥ 0; mode switch refused while balances exist. |
| test_manual_booking | Links only to a single verified phone match [D3]; an unverified match leaves `client_id` NULL; completion schedules `mcf` and earns only after "Ha"; "Yo'q" writes an audit row. |
| test_notifications | Confirm creates r24 and r2; a booking made under 24 h ahead has no r24; cancel cancels the rows; `claim_due` sets `claimed_at` and a second claim returns nothing [A4]; stale cutoffs per kind [C5]; r2 skipped after ack; ret skipped if the client rebooked; restart catch-up within the stale window. |
| test_schedule_conflicts | Preview; `cancel_conflicts`; keep returns `overlapping_kept`; hours shrink returns `out_of_hours`; `busy_now` grid rounding and no cancellations; barber blocked by setting; shop-wide is admin only. |
| test_barbers_invite | Token TTL 24 h; request → approve links; deny; reused or expired token; a second user can't claim; unlinked barbers hidden; `mark_reachable_by_user`; deactivate cancels future bookings. |
| test_users_admin | Env admin vs promoted; demote works and env admin can't be demoted [G2]; setup token flow [G1]; block cancels active bookings and refuses staff/self; `delete_me` anonymises and keeps counts [H1]; phone search writes an audit row. |
| test_permissions | Barber B can't act on or **read** Ali's bookings (`get_booking_for_actor`) [D5]; a barber sees masked phones on old bookings [H2]; a client can't touch others' bookings; non-admins refused by every admin service. |
| test_stats | Local-day bucketing with bookings at 23:30 and 00:30 local [C6]; revenue = SUM(final) of COMPLETED; manual share. |
| test_settings | Typed parse; invalid values; cross-field rules; markers. |
| test_migrations | `alembic upgrade head` on an empty file DB, then `alembic check` shows no diff against models. |
| test_backup | `backup_sqlite` creates a copy that passes `integrity_check` and keeps 14. |
| test_i18n, test_callbacks, test_bot_smoke | Key/placeholder parity; error-code coverage; ≤ 64 bytes for every callback; router builds; the AST no-I/O-in-tx check. |

**Definition of done:**
- `ruff check .` is clean.
- `pytest -q` is green on Python 3.10 and 3.12, and the race test passes 50 runs in a row.
- `python -m app.db.migrate && python -m app.bot.main` with a real token completes PLAN.md §5 by hand.
