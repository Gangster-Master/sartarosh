# R&D report: Telegram bot for barbershop bookings

## 1. What is on GitHub (searched 2026-10-24 through the GitHub search API)

This niche is small. No repo for barbershop or salon booking bots has more than 35 stars, and nothing exists for Uzbek. A search for "sartarosh" only finds web, CRM and mobile projects, plus one unlicensed Python bot with no description.

| Repo | Stars | Last push | License | Stack | Fit with our requirements |
|---|---|---|---|---|---|
| [Xaveron/barbershop-bot](https://github.com/Xaveron/barbershop-bot) | 1 | 2026-09-24 | MIT | Py 3.12, aiogram 3, PG16, SQLAlchemy 2 async, Alembic, APScheduler, Redis | **Closest match.** Several barbers, services, free slots, a Postgres `EXCLUDE USING gist` rule against double booking, reminders 24h and 2h before, `/admin`, tests and CI. **Missing:** bonus/loyalty, Uzbek (it has ru/ro/en). It is a multi-tenant SaaS with tariffs and a `/platform` layer, which is far more than we need. Created 2026-09, 32 commits, 0 forks, one author. |
| [DigitalJacob/booking_bot](https://github.com/DigitalJacob/booking_bot) | 5 | 2026-10-19 | MIT | Py 3.13, aiogram 3.20, PG17, Redis FSM, Docker | Good code quality: layered, business logic kept apart, tests, CI, the master accepts or declines from the notification, reminders, time off, walk-in bookings. **But it is built for one master**, so clients cannot choose a barber. No bonus system, no Uzbek. Created 2026-08. |
| [nazgool97/salon_bot](https://github.com/nazgool97/salon_bot) | 2 | 2026-01-25 | NOASSERTION (open-core) | aiogram 3, FastAPI, React Mini App | Has many features, but it is open-core: the full version is sold on Gumroad. It also uses a web Mini App, which breaks our "Telegram bot only" rule. Rejected. |
| [nazgool97/telegram-salon-booking-bot](https://github.com/nazgool97/telegram-salon-booking-bot) | 4 | 2026-01-14 | none | n/a | A showcase page only. Its README says "does NOT contain source code". Rejected. |
| [frolovelo/saloon_bot](https://github.com/frolovelo/saloon_bot) | 35 | 2025-04-30 | **none** | pyTelegramBotAPI 4.12, Google Sheets | The most-starred one. It has no license, so we cannot legally reuse it. It stores data in Google Sheets, has no barber accept step and no bonus system. Rejected. |
| [6HosT-io/VovaBarber](https://github.com/6HosT-io/VovaBarber) | 1 | 2026-10-13 | Apache-2.0 | Python | A small bot for one shop. Too basic. |
| [AinurMingazov/aiogram_barber_bot](https://github.com/AinurMingazov/aiogram_barber_bot) | 2 | 2024-07 | MIT | aiogram | Stale and minimal. |
| [ilnrzakirov/barber-bot](https://github.com/ilnrzakirov/barber-bot), [Robert-Vardanyan/Barber-Shop-Bot](https://github.com/Robert-Vardanyan/Barber-Shop-Bot), [urdrops/barbershop-bot](https://github.com/urdrops/barbershop-bot) | ≤3 | 2022–2025 | none | aiogram / other | No license, stale, or a demo for one barber. Rejected. |

## 2. Verdict: build our own and borrow ideas

No candidate is good enough to use as it is:
- **None has a bonus/loyalty system**, and none has Uzbek.
- The two well-engineered MIT projects are both less than 2 months old, each has one maintainer, and neither has a community (0–1 forks).
- DigitalJacob/booking_bot only handles one master, so the barber-choice step would have to be rebuilt from the core up.
- Xaveron/barbershop-bot supports several barbers, but its multi-tenant SaaS layer would have to be removed. Removing it, translating to Uzbek and adding loyalty is about as much work as writing a focused bot ourselves, and we would be maintaining code we did not write.

Both are MIT-licensed, so we can read them and borrow ideas freely (keep the license notice if we copy any code). Ideas worth taking:
1. **Double-booking protection inside the database.** Use a Postgres `EXCLUDE USING gist (barber_id WITH =, tstzrange(start,end) WITH &&) WHERE status IN ('pending','confirmed')`, plus a pending booking that holds the slot for N minutes while the barber decides. (Xaveron)
2. **One DB transaction per update**, done through middleware, and business logic in services that know nothing about aiogram, so it can be unit-tested. (DigitalJacob)
3. **A `notifications` table that stores reminders**, scanned by the scheduler, so reminders survive a restart. Do not keep reminder jobs only in memory. (Xaveron)
4. **Accept / Decline buttons directly on the barber's new-booking message**, with an optional reason. Cancel and decline both ask for confirmation. (DigitalJacob)
5. **Walk-in bookings created by the barber** and **barber time-off and breaks** that block slots. (DigitalJacob)
6. Weekly working hours plus exceptions for days off, a booking horizon in days, and a slot step taken from service length. (both)

What we add ourselves: Uzbek UI with Latin strings in one locale file, a loyalty ledger (points per completed visit, redeemed as a % discount or a free haircut after N visits), done/no-show marking, and a simple single-shop admin.

## 3. Recommended stack (works on Python 3.10.12, which is installed)

**Python + aiogram 3.** This is the most common stack for the bots above and has the largest Russian/Uzbek developer community. Use Node only if the team is JS-first; the equivalent would be grammY 1.x + Prisma + PostgreSQL, but nothing in this research favours it.

These versions are the newest on PyPI that still support Python 3.10:

| Purpose | Package | Version |
|---|---|---|
| Bot framework | `aiogram` | 3.31.0 (supports Python 3.10–3.14) |
| ORM (async) | `SQLAlchemy[asyncio]` | **2.0.55**. Do **not** use 2.1.x, it needs Python 3.11+. |
| Migrations | `alembic` | 1.19.x. The newest, 1.20.1, came out 2026-10-22; wait a couple of weeks before using it. |
| DB driver (production) | `asyncpg` | 0.32.0, with PostgreSQL 16 |
| DB driver (local dev / tests only) | `aiosqlite` | 0.22.1. The gist exclusion rule is Postgres-only, so tests that check overlaps must run on PG. |
| Config | `pydantic-settings` | **2.15.0**. Do not use 2.16.x, it needs Python 3.11+. |
| | `pydantic` | 2.14.x |
| Reminders and cleanup jobs | `APScheduler` | 3.11.3. Use the `AsyncIOScheduler` interval job that scans the `notifications` table. Avoid 4.x alphas. |
| FSM storage | `redis` | 8.1.0, with Redis 7. You can start with `MemoryStorage` for the MVP, but it loses booking dialogs on restart. |
| Tests | `pytest` 9.1.x, `pytest-asyncio` 1.4.0 | |
| Lint | `ruff` | latest |
| Deploy | Docker Compose (bot + postgres + redis) | Long polling for the MVP, webhook later |

Optional: `aiogram-dialog` 2.6.0 gives a widget-based calendar and time grid. Hand-built inline keyboards are simpler and easier to debug, so I recommend those.

Other technical decisions:
- Store times as `timestamptz` (UTC) and show them in `Asia/Tashkent` (UTC+5, no DST) using `zoneinfo`.
- Get the phone number with `KeyboardButton(request_contact=True)` and check that `contact.user_id == from_user.id`.
- A barber is a Telegram user linked through a one-time invite code or deep link, so we have their `chat_id` to send notifications to.
- Roles: client, barber, admin. Admin IDs come from env (`ADMIN_IDS`).
- Core tables: `users`, `barbers`, `services`, `barber_services`, `working_hours`, `time_off`, `bookings` (pending → confirmed / rejected / cancelled / done / no_show), `notifications`, `loyalty_ledger` (append-only, with balance = sum).
- Edge cases to handle: a pending booking expires automatically if the barber does not answer within X minutes, a rejection frees the slot, a cancellation less than N hours before the visit is blocked or flagged, no-shows earn no points, and points are spent only once a booking is confirmed or done.

## Sources
- https://github.com/Xaveron/barbershop-bot
- https://github.com/DigitalJacob/booking_bot
- https://github.com/nazgool97/salon_bot
- https://github.com/nazgool97/telegram-salon-booking-bot
- https://github.com/frolovelo/saloon_bot
- https://github.com/6HosT-io/VovaBarber
- https://github.com/AinurMingazov/aiogram_barber_bot
- https://github.com/ilnrzakirov/barber-bot
- https://github.com/Robert-Vardanyan/Barber-Shop-Bot
- https://github.com/urdrops/barbershop-bot
- PyPI JSON API (aiogram, SQLAlchemy, alembic, asyncpg, aiosqlite, pydantic-settings, APScheduler, redis), checked 2026-10-24

The `gh` CLI was not installed, so I used the public GitHub REST search API through curl. Search queries: barbershop telegram bot, barber bot aiogram, salon booking telegram bot, appointment telegram bot (with and without aiogram), telegram booking bot, booking bot aiogram, beauty salon telegram bot, sartarosh. The Cyrillic query "барбер бот" got no usable response from the API.
