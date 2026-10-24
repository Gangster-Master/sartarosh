# Sartarosh navbat bot

Telegram bot for booking appointments at one barbershop (sartaroshxona). Uzbek (Latin) is the
default language, Russian is available. Clients book in a few taps; barbers accept, reject or
propose another time; admins manage barbers, services, hours, days off, clients, settings and stats.

Architecture and contract: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Plan and acceptance
checklist: [`docs/PLAN.md`](docs/PLAN.md).

## What it does

**Clients**
- Booking wizard on one message: service → barber (or **🤝 Farqi yo'q**, any free barber) → day
  (Bugun / Ertaga / Indinga + quick days, month calendar for longer horizons) → time grouped as
  Ertalab / Kunduzi / Kechqurun → optional comment → phone via **📱 contact button** (first booking
  only) → summary with bonus toggle and name edit → ✅.
- ⬅️ back on every step keeps earlier choices; ❌ cancels at any step; stale buttons answer
  "Bu tugma eskirgan"; wizard state survives a bot restart.
- 🔁 "Avvalgidek yozilish" (same barber + service as last time), rebook buttons on every
  cancellation/rejection/return reminder.
- My bookings: cancel (with late-cancel warning), withdraw a pending request, answer a proposed
  time, "✅ Kelaman" from the 24 h reminder.
- Prices, barber cards with photo, address with weekly hours, venue pin and map links.
- Loyalty: stamp card (`✂️✂️✂️▫️▫️ 3/5`) or cashback, welcome bonus, referral link, rating.

**Barbers**
- New request with client name, phone, visits/no-shows, 🆕 badge and comment;
  `[✅ Qabul qilish] [❌ Rad etish] [🕒 Boshqa vaqt taklif qilish]`. Every copy of the request
  (barber and admins) is updated after a decision.
- 📅 Today / 🗓 upcoming days, booking cards with ✅ Bajarildi, 🚫 Kelmadi, ❌ Bekor qilish.
- ⏸ "Hozir bandman" (30/60 min), ⛔ block time (whole day or a range, conflict preview),
  ➕ manual walk-in/phone booking.
- Morning summary and nightly "unresolved bookings" digest.

**Admins**
- 💈 barbers (invite link + approval, step/buffer, auto-accept, services, deactivate with preview),
  🧾 services, 🕒 hours with breaks and "copy to all days", 🌴 days off (barber or whole shop),
  📋 bookings (filters, search, paging), 👥 clients (search, bonus ±, block, reset no-shows),
  📊 stats, ⚙️ settings (incl. location), 🛡 admins, 🔄 role switch.

## Requirements

- Python **3.10+** (production: 3.12, see `Dockerfile`)
- A bot token from [@BotFather](https://t.me/BotFather)

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock        # or requirements.txt
cp .env.example .env                    # set BOT_TOKEN and ADMIN_IDS (your Telegram id)
python -m app.db.migrate                # create / upgrade the SQLite schema (data/bot.db)
python -m app.bot.main                  # or: python run.py  /  python -m app
```

`python run.py --migrate` applies migrations and starts the bot in one go.

Optional demo data (refuses to run on a non-empty database):

```bash
python -m app.db.seed --demo
```

## Docker

```bash
cp .env.example .env    # fill BOT_TOKEN, ADMIN_IDS
mkdir -p data && sudo chown 10001:10001 data   # the container runs as uid 10001
docker compose up -d --build
docker compose logs -f bot
```

The container runs migrations and then the bot. `./data` holds the database, the lock file and
backups; it must be writable by uid 10001, otherwise migrations fail with "unable to open
database file" and the container restarts in a loop. Run exactly **one** instance: a second one
exits with code 3 ("already running"). There is no healthcheck: `restart: unless-stopped`
restarts the bot if the process dies.

Commands inside the container:

```bash
docker compose exec bot python -m app.ops.backup      # manual backup
docker compose exec bot python -m app.db.seed --demo  # demo data (empty DB only)
```

## Update

Make a backup first (see below), then:

```bash
# Docker (migrations run automatically on start)
git pull && docker compose up -d --build

# Local / systemd
git pull && .venv/bin/pip install -r requirements.lock && .venv/bin/python run.py --migrate
```

### systemd (without Docker)

```ini
# /etc/systemd/system/sartarosh.service
[Unit]
Description=Sartarosh navbat bot
After=network-online.target

[Service]
User=sartarosh
WorkingDirectory=/opt/sartarosh
ExecStart=/opt/sartarosh/.venv/bin/python run.py --migrate
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now sartarosh`, logs: `journalctl -u sartarosh -f`.

## First start

1. **Admin.** Put your Telegram id into `ADMIN_IDS` (find it with `/whoami` in the bot). If
   `ADMIN_IDS` is empty and nobody is admin yet, the startup log prints a one-time link
   `https://t.me/<bot>?start=setup_<token>` — the first person to open it becomes admin.
   Treat this link as a password: anyone who can read the logs can claim it. In production set
   `ADMIN_IDS` instead. Invalid `ADMIN_IDS` entries are logged as warnings at startup.
2. **Shop info.** ⚙️ Sozlamalar → 🏪 Sartaroshxona: name, address, phone, map links; send the
   location with 📍.
3. **Services.** 🧾 Xizmatlar → ➕ (e.g. "Soch olish", 30 min, 60 000 so'm).
4. **Barbers.** 💈 Sartaroshlar → ➕ → the card shows an invite link (valid 24 h). Send it to the
   barber; when they open it, every admin gets **Tasdiqlash / Rad etish**. A barber is visible to
   clients only after approval.
5. **Hours.** 🕒 Ish vaqti → barber → weekday → `10:00-20:00`, break `13:00-14:00`, then
   "📋 Hamma kunlarga nusxa".

All business rules (booking horizon, lead time, cancellation window, reminders, no-show policy,
loyalty) are in ⚙️ Sozlamalar and apply on the next scheduler tick, no restart needed.

## Commands

| Command | Who | |
|---|---|---|
| `/start` | all | start, deep links `inv_…`, `setup_…`, `ref_…`, `src_…` |
| `/menu` | all | fresh main menu (also leaves any flow) |
| `/cancel` | all | cancel the current flow |
| `/my` | client | my bookings |
| `/today` | barber | today's queue |
| `/lang` | all | language |
| `/whoami` | all | Telegram id, roles |
| `/help` | all | help per role |
| `/delete_me` | all | delete personal data (bookings are cancelled) |

## Backups and restore

The bot makes a daily SQLite online backup at `backup_time` (default 03:30) into
`data/backups/` (14 kept) and sends it to the `ADMIN_IDS` admins. **Privacy:** the backup is the
whole database, including client names and phone numbers, so keep `ADMIN_IDS` to people who may
see that data. Manual backup:

```bash
python -m app.ops.backup                          # local
docker compose exec bot python -m app.ops.backup  # Docker
```

Restore: stop the bot, remove the SQLite WAL files (stale ones would corrupt the restored
database), copy the backup over the database, start the bot.

```bash
docker compose stop bot            # or: sudo systemctl stop sartarosh
rm -f data/bot.db-wal data/bot.db-shm
cp data/backups/bot-YYYY-MM-DD.db data/bot.db
sudo chown 10001:10001 data/bot.db  # Docker only
docker compose start bot           # or: sudo systemctl start sartarosh
```

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `BOT_TOKEN` | — | required |
| `ADMIN_IDS` | — | comma-separated Telegram ids; cannot be demoted from the bot |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/bot.db` | relative paths resolve against the project root |
| `DATA_DIR` | `data` | lock file and backups |
| `SHOP_TZ` | `Asia/Tashkent` | shop time zone (never use `TZ`) |
| `LOG_LEVEL` | `INFO` | |
| `REDIS_URL` | — | optional FSM storage; by default the FSM is stored in the database |
| `SCHEDULER_TICK_SECONDS` | `30` | |
| `THROTTLE_PER_MINUTE` | `30` | per-user message + button limit |

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

Layout: `app/services` (business logic, no aiogram), `app/db` (models, sessions, migrations),
`app/bot` (aiogram handlers, keyboards, locales, middlewares, notifier, scheduler).
Every user-visible string lives in `app/bot/locales/uz.py` (and the same key in `ru.py`).
