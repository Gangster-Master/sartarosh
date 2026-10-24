# Sartarosh Navbat Bot: delivery plan (final)

**Status:** FINAL, issued by the Judge on 2026-10-24. This plan replaces the PM draft.

**Inputs:**
- REQUIREMENTS.md (final)
- ARCHITECTURE.md (final, binding)
- IDEAS.md §12 (which GitHub ideas are adopted)
- RND.md

**Owner's instruction:** good features from the GitHub projects must be adopted. Every idea the R&D catalogue marked MVP is now in v1.0, and some cheap v1.1 ideas were pulled in too (soft-block/no-show approval #68, the ban guard rails #69, the booking source #78). The rest are on the roadmap (§9).

**Target:**
- v1.0 in **8 working days plus a 2-day stabilisation buffer**. The draft planned 5+1; the adopted features and the critic fixes add about 40% more work.
- Code targets Python 3.10.12; production runs on python:3.12-slim.
- aiogram 3.31, SQLAlchemy 2.0.55 async, SQLite WAL, Alembic.

**Team:**
- BACKEND: services, DB, ops.
- BOT/FRONT: the aiogram layer and locales.
- QA: tests and manual end-to-end runs.
- CTO: approves contract changes and is the technical judge.
- PM: scope, tracking, acceptance.
- Negative reviewer: attacks every gate.
- R&D: spikes, deploy files, the backup module.

---

## 0. Ground rules

1. **The contract freezes at M0.** ARCHITECTURE §4 (signatures), §3 (schema), §7 (callbacks) and §8 (FSM) are fixed then. Changes follow §7 of this plan.
2. **CI boundary checks (T-CI-2):**
   - `app/services`, `app/db`, `app/ops` and `app/config.py` do not import `aiogram`.
   - `app/bot` does not import `app.db.models` or build SQL.
   - Handlers do not call the functions marked `# internal`.
   - The AST check finds no Telegram I/O inside a `write_tx`/`read_tx` block.
3. **Python 3.10 compatible:** ruff target `py310`. CI runs the tests on 3.10 and 3.12.
4. **One branch per task, small PRs, and a cross-role review:**
   - BACKEND PRs are reviewed by QA.
   - BOT PRs are reviewed by BACKEND.
   - QA PRs are reviewed by BACKEND.
   - P0 areas (`session.py`/`uow.py`, `create_booking`, the state machine, `claim_due`, bonus) are also reviewed by the CTO.
5. **Meetings:** a 15-minute daily sync, and a gate review at every milestone. At each gate the Negative reviewer presents at least 3 attacks.
6. **Definition of Ready:** the signature is in the stubs, its DTOs exist, and the acceptance IDs are listed.
7. **P2 cut line:** the P2 items are the Russian locale, cashback mode, referral, the month calendar, and rating. They are built **last** (Days 7–8). At gate M4 the PM may move any unfinished P2 item to v1.1 without asking the owner. Their schema columns, DTOs, signatures and tests stay, and the tests are marked `xfail(reason="P2 cut")` only for the cut items.

---

## 1. Milestones

| ID | Name | Day | Exit criteria (gate) |
|---|---|---|---|
| **M0** | Contract freeze | Day 1 (first half) | The repo tree matches ARCH §2. `requirements.txt` installs in clean 3.10 and 3.12 venvs, and `requirements.lock` is frozen. `enums`, `errors`, `dto` are complete and every §4 signature is stubbed. `ruff` is clean, `pytest` collects, CI boundary checks are green, and the QA-01 parity list is checked in. |
| **M1** | Foundations | End of Day 1 | `config`, `db/*` (`base`, `types`, `models`, `session`, `uow`, `migrate`), the Alembic baseline and `clock`/`validation` work for real. `main.py` starts with all 5 startup checks, the lock file, `DbStorage`, the middlewares, and a hub rendered from stub data. `test_validation`, `test_clock`, `test_migrations` and `test_uow` are green. |
| **M2** | Booking engine and client flow | End of Day 4 | `slots` (including "any barber"), `bookings` (create, the full state machine, proposals), `notifications` (claim model), `users` core and `catalog` core are done. Client stories C0–C13 and C16 work end to end, as do barber stories B1–B3. The race, state machine, proposal, limit, expiry, cancel and notification tests are green. |
| **M3** | Barber tooling, scheduler, loyalty | End of Day 6 | `barbers` (approval linking), `schedule` (incl. busy-now), stamps loyalty (incl. welcome and the manual-visit confirm) and `settings` are done. B4–B12 and C14, C15, C17, C19 work. The tick, markers, auto-complete and backup run. The bonus, manual booking, conflicts, invite and permission tests are green. |
| **M4** | Admin, P2, end to end | End of Day 8 | A0–A10 are done. The P2 items are built, or cut per rule 0.7. `seed --demo` works. The whole §5 checklist has been run once and every bug is logged. |
| **M5** | Stabilisation and release | Days 9–10 | Every P0 and P1 bug is fixed. The Definition of Done (§4) is met. The README and deploy files are verified on a clean machine. The CTO and PM sign off. |

---

## 2. Task breakdown

Estimates are in ideal hours. "Dep" lists the tasks a task depends on. A task ID with `*` is new or changed by the Judge.

### 2.1 BACKEND

| ID | Task | Est | Dep | Done when |
|---|---|---|---|---|
| BE-01* | Skeleton: tree, `pyproject` (py310), requirements, dev requirements, **lock file from clean 3.10 and 3.12 venvs** [G6], `.env.example` (`SHOP_TZ`), `.gitignore`, `THIRD_PARTY_NOTICES` stub | 2 | – | Installs on both versions |
| BE-02 | `enums`, `errors` (`InputError` naming [G8]), `dto` (all of §4.2) | 3 | BE-01 | Imports cleanly; the codes match §4.1 |
| BE-03 | Stubs for every §4 signature | 2.5 | BE-02 | **M0 gate.** QA-01 parity check green |
| BE-04* | `config.py`: `SHOP_TZ`, optional token, `PROJECT_ROOT`-relative paths, `CONSENT_VERSION` [C1][C3][G4] | 1 | BE-01 | Unit test |
| BE-05 | `db/base`, `db/types` (`UTCDateTime`, `StrEnumType`) | 1.5 | BE-01 | Round-trip test |
| BE-06* | `db/models`: every table in §3, including `booking_slots`, `booking_messages`, `fsm_states`, `reviews`, the new booking columns, and the ledger's unique keys | 5 | BE-05 | `create_all` works; CHECK and UNIQUE constraints fire |
| BE-07* | `session.py` (begin-mode listener, absolute path), `uow.py` (`write_tx`/`read_tx`) [A1][A2] | 3 | BE-06 | `test_uow` green |
| BE-07b* | Alembic env (async, batch mode), baseline `0001`, `migrate.py` with `assert_schema_current` [G7][#94] | 2 | BE-06 | `test_migrations` green |
| BE-08* | `clock.py` (lazy tz, grid helpers, `clamp_to_daytime`), `validation.py` (comment, `mask_phone`) | 2 | BE-02 | `test_validation`, `test_clock` green |
| BE-09* | `settings.py`: specs, cross-field rules, loyalty-mode guard, markers, setup token | 3 | BE-07 | `test_settings` green |
| BE-10 | `audit.write_audit` | 0.5 | BE-07 | – |
| BE-11* | `slots.compute_free_slots` with buffer and end-of-block candidates [F4][#28] | 2.5 | BE-08 | `test_slots_pure` green |
| BE-12* | `slots` wrappers, `get_free_slots_any`, `list_proposal_slots` [#1][#21] | 4 | BE-11, BE-15a | `test_slots_db` green; 7 days for one barber < 50 ms |
| BE-13* | `users` core: `load_context`, `needs_sync`, `ensure_user` (ref/src payloads), roles (env vs promoted), language, consent, verified contact, soft-block, stats | 4 | BE-08 | – |
| BE-14* | `bookings.create_booking` (all steps in §4.11, "any barber" loop, per-phone limits), `compute_expire_at` (new formula), `create_manual_booking` (verified-phone linking), decision-message storage | 8 | BE-12, BE-13 | Create, race, limit and expiry tests green |
| BE-15a | `catalog` resolve/list helpers needed by slots | 1.5 | BE-06 | – |
| BE-16* | State machine incl. late accept, proposals, `_occupy`/`_release`, `_complete` (bonus fallback), the no-show flip, `confirm_manual_visit`, `expire_due_pending`, `auto_complete_due` | 8 | BE-14, BE-19 | State machine, proposal, cancel and no-show tests green |
| BE-17* | Queries: `get_booking_for_actor` (masking), `list_client_bookings`, `get_last_completed`, `list_barber_day`, `list_bookings` (filters, paging), `list_unresolved`, `list_pending_digest`, `bookings_for_daily_summary` | 3.5 | BE-14 | `test_permissions` green |
| BE-18* | `notifications.py`: schedule rules for all 6 kinds, claim with stale and invalid filters, mark sent/failed [A4][C5] | 4 | BE-16 | `test_notifications` green |
| BE-19* | `bonus.py`: stamps (the cashback branch is P2), reserve/quote, earn with day cap, welcome, reversal, adjust, recompute. Referral is P2. | 5 | BE-07 | `test_bonus` green |
| BE-20* | `barbers.py`: CRUD, 24 h invite, request/approve/deny link, deactivate (always cancels), reachability recovery | 3.5 | BE-16 | `test_barbers_invite` green |
| BE-21 | `catalog` admin parts | 2 | BE-15a | – |
| BE-22* | `schedule.py`: hours, copy, shop week hours, time off (grid), `busy_now`, conflicts | 4.5 | BE-16 | `test_schedule_conflicts` green |
| BE-23* | `users` admin parts: phone search with audit, profile, block (cancels bookings, guard rails), reset, promote/demote, admin list, `delete_me`, `claim_setup_token` | 3.5 | BE-13, BE-16 | `test_users_admin` green |
| BE-24* | `stats.get_stats`: local-day buckets, top services, manual share | 2 | BE-16 | `test_stats` green |
| BE-25* | `reviews.py` [P2], `fsm_store.py` | 1.5 | BE-07 | – |
| BE-26* | `seed.py --demo` with its guards [G3] | 1.5 | BE-20..22 | Refuses without the flag |
| BE-27 | README: install, `.env`, migrate, run, first-admin setup link, invite and approve a barber, backups and restore | 1.5 | M4 | PM review |
| BE-28 | Bug-fix pool | 8 | – | – |

**BACKEND total is about 99 hours.** This is the critical path. Mitigations, decided now:
- R&D takes `ops/backup.py`, `deploy/*` and `tools/backup.sh`.
- BOT takes BE-21, BE-24 and BE-26 if BACKEND is more than half a day behind at M2.
- QA owns the parity list and factories.

### 2.2 BOT/FRONT

| ID | Task | Est | Dep | Done when |
|---|---|---|---|---|
| FE-01* | `i18n.py`, `locales/uz.py` (every key), `ru.py` skeleton with the same keys (the Russian text is P2), the `fmt_*` helpers, `h()` | 5 | BE-02 | `test_i18n` green |
| FE-02* | `callbacks.py` (§7, including `SlotCb` with b and s), `states.py` (§8) | 2 | – | `test_callbacks` ≤ 64 bytes |
| FE-03* | `screen.render` (text↔photo switch, not-modified), keyboards/common (role menus, contact, back/cancel) [E4] | 3 | FE-01 | – |
| FE-04* | Middlewares: private, throttle (TTL, messages too), debounce, user (read-then-write sync, reachability recovery, consent gate, exits) [A2][B4][E1][E7][H3] | 4 | BE-03 | – |
| FE-05* | `main.py` (startup order and checks, lock, `DbStorage`, commands per language), `fsm_storage.DbStorage`, filters, `build_root_router` | 3.5 | FE-04 | Bot starts on stubs |
| FE-06* | `onboarding.py`: language, consent, payloads (`inv_`, `setup_`), `/whoami`, `/menu`, `/cancel`, `/delete_me`, `/help`; `errors.py` handler | 3 | FE-05 | – |
| FE-07* | `notifier.py`: every method in §6.4, multi-target decision messages, per-recipient language, pacing | 5 | FE-01 | – |
| FE-08* | Client wizard: service-first and barber-first entry, any-barber, quick days, grouped slots, comment, contact step with new-message handover, summary with bonus toggle and name edit, `msg_id` guard, nav stack | 10 | FE-03, BE-12, BE-14 | CL-01..CL-16 pass |
| FE-09* | `client_my.py`: list, cancel and withdraw, proposal answers, Kelaman, manual-visit Ha/Yo'q, rebook entry | 4 | FE-08, BE-16 | – |
| FE-10* | `client_info.py`: hub with "🔁 Avvalgidek", prices, barber cards with photos, address with venue/map buttons/generated hours, bonus page with stamps | 3.5 | BE-09, BE-19, BE-22 | – |
| FE-11* | `barber_decide.py`: accept, reject with reasons, propose picker; admins allowed; stale handling | 4 | FE-07, BE-16 | – |
| FE-12* | `barber_queue.py`: today, upcoming, booking card, done, no-show and flip, staff cancel | 3.5 | BE-17 | – |
| FE-13* | `barber_block.py`: `BlockTimeFlow` and busy-now | 3.5 | BE-22 | – |
| FE-14 | `barber_manual.py` | 3 | BE-14 | – |
| FE-15* | `scheduler.py`: tick steps 1–5, markers, backup send | 3.5 | FE-07, BE-18 | – |
| FE-16* | Admin barbers (incl. link approvals, badges), admins section | 4 | BE-20, BE-23 | – |
| FE-17 | Admin services, hours, time off | 4 | BE-21, BE-22 | – |
| FE-18* | Admin bookings (filters, search, paging), clients, settings (grouped, location), stats | 6 | BE-09, BE-23, BE-24 | – |
| FE-19 [P2] | Russian text for every key; month calendar; cashback UI; referral share; rating | 7 | M3 | – |
| FE-20 | Smoke tests (router build, AST no-I/O check) | 1.5 | – | – |
| FE-21 | Bug-fix pool | 7 | – | – |

**BOT total is about 91 hours, P2 included.**

### 2.3 QA

| ID | Task | Est | Dep |
|---|---|---|---|
| QA-01 | Contract parity test (introspects signatures against a checked-in list) | 1.5 | BE-03 |
| QA-02* | `conftest` (env before import, in-memory and file DBs, `NOW`), `factories` | 3 | BE-03 |
| QA-03* | `test_validation`, `test_clock`, `test_slots_pure` | 4 | QA-02 |
| QA-04* | `test_uow`, `test_migrations`, `test_slots_db`, `test_booking_create` | 5 | BE-07, BE-14 |
| QA-05* | `test_booking_race` incl. the orphan-row assertion; 50-run stability | 3 | BE-14 |
| QA-06* | `test_booking_state_machine`, `test_proposals` | 6 | BE-16 |
| QA-07* | `test_booking_limits`, `test_pending_expiry` (5 worked examples), `test_cancel_policy`, `test_no_show_softblock` | 5 | BE-16 |
| QA-08* | `test_bonus`, `test_manual_booking`, `test_notifications` | 6 | BE-18, BE-19 |
| QA-09* | `test_schedule_conflicts`, `test_barbers_invite`, `test_users_admin`, `test_permissions`, `test_stats`, `test_settings`, `test_backup` | 7 | BE-20..24 |
| QA-10 | Manual end to end with a real token and 4 accounts (admin, barber A, barber B, client), following §5 | 10 | M4 |
| QA-11 | Regression pass | 4 | M5 |

**QA total is about 55 hours.**

### 2.4 PM, CTO, Negative reviewer, R&D

| ID | Owner | Task |
|---|---|---|
| PM-01 | PM | Board, daily sync, gates, scope control (anything not in REQUIREMENTS §10 goes to the §9 roadmap), the P2 cut decision at M4 |
| PM-02 | PM | Get the owner's answers to Q9–Q12 by Day 3 (hosting, live queue, new-client approval, bonus amounts) |
| CTO-01 | CTO | Rule on change requests within 2 h |
| CTO-02 | CTO | Review the P0 areas (rule 0.4) |
| NEG-01 | Negative reviewer | Each gate: races, timezone edges, stale and forged callbacks, permission bypass on reads, HTML injection, bonus double-spend and farming, the restart burst, two instances |
| RND-01* | R&D | Day 2: confirm that the begin-mode execution option reaches the `begin` event under aiosqlite (fallback: two engines), how `BEGIN IMMEDIATE` and `busy_timeout` behave under 20 concurrent writers, and the APScheduler/aiogram shutdown order |
| RND-02* | R&D | `ops/backup.py`, `tools/backup.sh`, `deploy/Dockerfile`, `deploy/sartarosh.service`, log rotation |
| RND-03 | R&D | Postgres readiness note for v1.1 (`FOR UPDATE`, `EXCLUDE` as an optional second guard) |
| RND-04* | R&D | Check that the THIRD_PARTY_NOTICES entries match any adapted MIT code |

---

## 3. Spec ambiguities: final rulings (these replace the PM draft's §3)

1. **Pending timeout:** the new `compute_expire_at` in ARCH §4.11 (cutoff = start − 10 min, a decision window of at least 15 min, the morning digest for night bookings). The 12 h formula in the requirements draft and the CTO formula are both withdrawn [B1][B2].
2. **Bonus progress:** `progress = min(available, required)`, shown as `✂️…▫️ progress/required` followed by "(required+1)-chi soch olish bepul".
3. **Late cancel:** cancel is always offered on future active bookings. A cancel is late only for CONFIRMED bookings inside the window [D6].
4. **Python:** the code targets 3.10; production runs on 3.12 [G6].
5. **`full_day_interval`:** now **sync**, because it is pure. The PM draft's ruling is reversed.
6. **Nightly job:** a digest at 23:00, then an auto-complete after 12 h with a 24 h no-show flip [#30]. The PM draft's "never automatic" is reversed, because the owner asked for the GitHub features and unresolved bookings would otherwise block loyalty.
7. **"Farqi yo'q" (any barber):** **in the MVP** [#1]. The PM draft's v1.1 ruling is reversed.
8. **Reminder offsets:** 24 h and 2 h for clients (per #35). The client 1 h reminder and the barber 1 h ping are dropped. The barber gets the morning agenda; the barber 1 h ping is v1.1 [#32].
9. **Booking flow order:** service first, then the barber. Barber-first is available from the barber card [#1][#2].
10. **Default auto-accept:** on. New and soft-blocked clients still need approval [B3][D1].

---

## 4. Definition of Done

### 4.1 Per task
- The code matches the ARCH contract, and the CI boundary and no-I/O checks pass.
- `ruff` is clean. `pytest` is green on 3.10 and 3.12.
- New logic has tests. Every new string is in `uz.py`, and `ru.py` has the same key. User values go through `h()`.
- A peer review is approved, and the CTO has reviewed it if it touches a P0 area.

### 4.2 Release v1.0
1. `ruff` is clean, `pytest` is green on both Python versions, and the race test passes 50 runs in a row.
2. On a clean machine, following only the README: `pip install -r requirements.lock`, then `python -m app.db.migrate`, then `python -m app.bot.main`. The first-admin setup link works.
3. Every §5 scenario passes with a real token. No P0 or P1 bug is open.
4. Restarting the bot mid-flow loses no wizard state (DbStorage), sends no reminder twice, and sends no burst of stale reminders.
5. A second instance on the same machine refuses to start.
6. The nightly backup is produced and a test restore works.
7. No secrets are in the repo. `.env.example` is complete. `THIRD_PARTY_NOTICES` is accurate.
8. The CTO signs off on conformance and the PM on the acceptance checklist.

**Bug severity:**
- **P0:** double booking, orphan rows, data loss, a crash loop, a permission bypass (including reads), a wrong time shown, bonus double-spend.
- **P1:** a broken user story.
- **P2:** cosmetic issues or wording.

---

## 5. Acceptance checklist (manual in Telegram; automated equivalents noted)

**Setup:**
- Run `migrate`. The admin comes from `ADMIN_IDS`; the setup-link flow is tested on a separate DB.
- Barbers A (Ali) and B (Bekzod) are linked and approved.
- Clients C1 and C2 use separate accounts.
- Hours are Mon–Sat 10:00–20:00 with a 13:00–14:00 break.
- Services: "Soch olish" 30 min at 60 000 so'm, "Soch + soqol" 60 min.

### 5.1 Onboarding, roles, admin bootstrap
- [ ] **ON-01** A new user's `/start` with Telegram set to English shows the language picker. After picking a language, the consent screen appears. Before consent, every menu button re-shows the consent screen.
- [ ] **ON-02** Telegram set to Russian skips the picker and shows the consent screen in Russian [P2: falls back to Uzbek text if ru is cut].
- [ ] **ON-03** On an empty admin DB, the startup log prints the `setup_` link. The first person to open it becomes admin. A second use of the link gives `invalid_invite` [G1].
- [ ] **ON-04** `/whoami` shows the tg id and roles.
- [ ] **ON-05** The admin creates "Ali" and gets an invite link. Account A opens it and sees "Tasdiq kutilmoqda" (waiting for approval). The admin gets Approve/Deny and approves. A now sees the barber menu. A forwarded link opened by someone else after A used it gives `invalid_invite`. A link older than 24 h is refused [D4] (automated).
- [ ] **ON-06** Ali is not visible to clients before linking [B4].
- [ ] **ON-07** A demoted promoted admin loses the admin menu on their next tap. An env admin cannot be demoted from the bot [G2].
- [ ] **ON-08** Adding the bot to a group makes it leave. Messages in groups are ignored [E7].
- [ ] **ON-09** A blocked user gets the `blocked` text, at most once per 10 minutes.
- [ ] **ON-10** `/start ref_<code>` from a new account stores the referrer. A user's own code is ignored [P2].

### 5.2 Client booking
- [ ] **CL-01** A returning client with a stored phone books in 6 taps or fewer. "🔁 Avvalgidek" reaches the day step in 1 tap [#15].
- [ ] **CL-02** Every wizard step edits the same message. Opening a barber card with a photo and going back works without errors [E4].
- [ ] **CL-03** Services show price and duration. If the shop has no services, the service step is skipped.
- [ ] **CL-04** "🤝 Farqi yo'q" shows the union of free times. Confirming assigns the least-loaded barber, and the summary and confirmation name that barber [#1].
- [ ] **CL-05** From a barber card, "✂️ Shu ustaga yozilish" lists only that barber's services [#2].
- [ ] **CL-06** The day buttons read Bugun / Ertaga / Indinga, then labels such as `Pa 29.10`, derived from the real calendar. Sunday shows "· band" and an alert. After 19:30, "Bugun" is hidden for a 30-minute service.
- [ ] **CL-07** Slots appear under the Ertalab, Kunduzi and Kechqurun headers, 4 per row. Nothing is offered during the break, inside a block, before now + 30 min, or past the horizon. A 60-minute service is not offered at 12:30 or 19:30.
- [ ] **CL-08** ⬅️ on every step returns to the previous step with the earlier choices kept. ❌ ends the wizard at any step [#8][#9].
- [ ] **CL-09** The comment step: "Izohsiz davom etish" skips it. A comment `<b>soqol</b>` reaches the barber as literal text.
- [ ] **CL-10** First booking with no phone: the contact keyboard appears. Sharing your own contact is accepted. Forwarding someone else's contact is refused, and typing `90 123 45 67` gets "use the button" [D1]. After the contact is shared, the main menu keyboard comes back. The summary is a new message, and tapping a slot on the old wizard message answers STALE [E2].
- [ ] **CL-11** The summary shows `26-oktabr, dushanba, 10:30` (the correct weekday). "✏️ Ismni o'zgartirish" accepts `Gʻayrat` and rejects `A` and `<b>x</b>`.
- [ ] **CL-12** A new client with an auto-accept barber: the booking is PENDING and the result shows "↩️ So'rovni qaytarib olish". A client with a completed visit gets CONFIRMED at once, with the address and pin [B3].
- [ ] **CL-13** Restarting the bot in the middle of the wizard, then tapping the next step, continues correctly (DbStorage). After the FSM row is deleted by hand, the tap answers `session_expired` [E3].
- [ ] **CL-14** Typing any text while on the name step, then tapping "📋 Mening navbatlarim", leaves the wizard and shows the list [E1].
- [ ] **CL-15** `/menu` and `/cancel` work from every state.
- [ ] **CL-16** "📍 Manzil va aloqa" shows the hours generated from the schedule, with "Bugun: … (ochiq/yopiq)". The venue pin appears only when a location is set. The map buttons appear only when their URLs are set [#59][#60][E8].

### 5.3 Race and stale data (P0)
- [ ] **RC-01** C1 and C2 confirm the same slot within 1 s. Exactly one booking is created. The other client gets `slot_taken` and a refreshed grid. `SELECT COUNT(*)` on bookings shows no extra row [A1] (automated).
- [ ] **RC-02** An overlapping 10:00/60 and 10:30/30 race has the same result.
- [ ] **RC-03** Waiting on the summary until the slot falls inside the lead time gives `slot_unavailable`.
- [ ] **RC-04** Old day or slot buttons give the STALE alert. A fast double tap on ✅ creates one booking [#10].
- [ ] **RC-05** While C1 is in the wizard and a reminder burst is running, C2's booking still responds in under 1 s (no write lock held across network calls) [A2].

### 5.4 Limits
- [ ] **LM-01** A 4th active booking gets `client_limit` (max_active=3).
- [ ] **LM-02** A 6th request in one day gets `client_limit` (max_per_day).
- [ ] **LM-03** A booking with B that overlaps one with A is refused. Booking 10:00 and 10:30 with Ali back to back is allowed [E5].
- [ ] **LM-04** A second account with the same verified phone counts toward the same limits [D1] (automated).
- [ ] **LM-05** About 40 quick actions, messages included, trigger `too_fast` [H3].

### 5.5 Barber decision
- [ ] **BD-01** The push shows name, phone (tappable text), service, time, visits, no-shows, 🆕 and the comment, with Accept, Reject and Propose buttons.
- [ ] **BD-02** Accept: every copy (barber and admin) is edited to "✅ Qabul qilindi". The client gets the confirmation with address and pin.
- [ ] **BD-03** Reject with "Kasal": the client gets the apology, the reason and a rebook button, and the slot is free again.
- [ ] **BD-04** "Boshqa…" takes free text, and the text reaches the client escaped.
- [ ] **BD-05** Propose: the barber sees up to 8 slots and picks 11:00. The client sees Accept/Decline. Accepting makes the booking CONFIRMED at 11:00, and 10:30 is free again. In a second run, declining gives CANCELLED_BY_CLIENT, not late. A third proposal is refused [#21].
- [ ] **BD-06** Accepting 5 min after expiry confirms the booking if the slot is still free, and the client is told. After 15 min, accepting gives `too_late` [B5].
- [ ] **BD-07** Barber B cannot accept **or open the card of** Ali's booking with a crafted callback [D5] (automated).
- [ ] **BD-08** Ali has blocked the bot: a new booking goes to the admins with decision buttons at once, and Ali is marked unreachable. When Ali sends `/start` again, he is reachable again [B4][#23].

### 5.6 Pending timeout and expiry
- [ ] **EX-01** During working hours with no answer: at 15 min the barber and the admins are pinged with buttons. The booking expires at min(created + 60, start − 10). The client gets a rebook button, and every decision message is edited.
- [ ] **EX-02** A Sunday 23:00 request for Monday 10:00 expires at 09:50. The 08:00 summary delivers it to Ali with buttons, and Ali accepts at 08:05 [B1] (automated expiry times per ARCH §10).
- [ ] **EX-03** A request made 35 min ahead, during hours, gets at least 15 min for the decision [B2].
- [ ] **EX-04** Settings refuses `min_lead_min=20` while the cutoff is 10 and the minimum decision time is 15.

### 5.7 My bookings, cancel, reminders, retention
- [ ] **MY-01** The list shows upcoming bookings and the last 5 past ones.
- [ ] **MY-02** A cancel more than 2 h ahead is free, the slot is freed, and the barber is notified.
- [ ] **MY-03** Cancelling a CONFIRMED booking 1 h ahead shows a warning and is counted late. Withdrawing a PENDING request 1 h ahead is not counted late [D6].
- [ ] **MY-04** A past booking has no cancel button, and a crafted callback is refused.
- [ ] **MY-05** For a booking more than 24 h ahead, the 24 h reminder arrives with Kelaman and Bekor qilish. Pressing Kelaman means the 2 h reminder is not sent [#35].
- [ ] **MY-06** Without Kelaman, the 2 h reminder arrives.
- [ ] **MY-07** A booking made 3 h ahead gets no 24 h reminder. A cancelled booking gets nothing.
- [ ] **MY-08** Restart catch-up: stop the bot from 30 min before a 2 h reminder until 10 min after it, then start it. The reminder is sent once. Stop it for 3 h instead and the reminder is dropped, not sent late [C5].
- [ ] **MY-09** Return reminder: with the setting at 1 week (QA), the client gets "Soch olish vaqti keldi" between 10:00 and 21:00 local, with rebook. A client who has already rebooked gets nothing [#37].

### 5.8 Barber operations
- [ ] **BQ-01** `/today` and "📅 Bugungi navbat" list the day in time order, with the phone as text.
- [ ] **BQ-02** "🗓 Kelgusi kunlar" lets the barber pick a day and see its list.
- [ ] **BQ-03** Done: COMPLETED, the client gets a thank-you with the new stamp count and a rebook button.
- [ ] **BQ-04** No-show before +15 min gives `too_early`. After that it is allowed, and the client gets a note.
- [ ] **BQ-05** Staff cancel requires a reason, and the client gets a rebook button.
- [ ] **BQ-06** "⏸ Hozir bandman 30" at 14:07 blocks 14:00–14:45 for clients and warns about an overlapping booking without cancelling it [F1].
- [ ] **BQ-07** Block 15:00–17:00 over a 15:30 booking: choosing "cancel" notifies the client with a rebook button; choosing "keep" leaves the booking. The 15:00–17:00 slots disappear in both cases. "Butun kun" removes the day.
- [ ] **BQ-08** With `barbers_can_block_time=False`, a barber's block is refused.
- [ ] **BQ-09** Walk-in with a phone that matches exactly one verified client: the booking is linked. Done sends the client "edingizmi?". "Ha" credits the stamp; "Yo'q" alerts the admins and credits nothing [D2]. A walk-in whose phone matches only an unverified client stays unlinked [D3].
- [ ] **BQ-10** A second completion for the same client on the same day credits no second stamp [D2].
- [ ] **BQ-11** The 08:00 summary arrives once. Restarting the bot at 08:30 does not resend it. Changing `daily_summary_time` takes effect the next day without a restart [C4].
- [ ] **BQ-12** The 23:00 digest lists unresolved bookings with buttons. If they stay unmarked, they are auto-completed at end + 12 h. The barber can flip one to "Aslida kelmadi" (didn't actually come) within 24 h, and the stamp is reversed [#30].

### 5.9 Loyalty
- [ ] **BN-01** The first completed visit gives +1 stamp plus the +1 welcome stamp. The bonus page shows `✂️✂️▫️▫️▫️ 2/5` [#45].
- [ ] **BN-02** With 5 or more available, the summary offers 🎁 with price 0. Completion debits 5 and credits no stamp.
- [ ] **BN-03** A cancelled or no-show bonus booking leaves the balance unchanged. While one bonus booking is active, a second bonus booking is offered only if `available` covers it.
- [ ] **BN-04** Raising the required stamps to 10 while a bonus booking is pending: completion still works at full price, and the barber sees "bonus qo'llanmadi" (bonus not applied) [A5].
- [ ] **BN-05** Percent mode at 50% halves the price.
- [ ] **BN-06** A manual adjustment without a comment is refused. With a comment it is applied and audited, and the balance never goes below 0.
- [ ] **BN-07** [P2] Cashback mode: switching is refused while balances exist. On a fresh DB, 5% is credited, and redeeming up to 50% works.
- [ ] **BN-08** [P2] Referral: a friend who joined through C1's link completes a first visit, and C1 is credited. An auto-completed first visit credits nothing.

### 5.10 Soft-block
- [ ] **SB-01** After 3 no-shows in 90 days, the client sees a warning, and bookings stay PENDING even with an auto-accept barber.
- [ ] **SB-02** "Reset no-shows" by the admin lifts the soft-block.

### 5.11 Admin
- [ ] **AD-01** Barber CRUD covers the photo, step, buffer of 15 (the next slot moves accordingly) and auto-accept. Deactivating previews the future bookings, cancels them with client notifications, and hides the barber.
- [ ] **AD-02** Services: a duration of 20 or a negative price is refused. A deactivated service disappears from the wizard.
- [ ] **AD-03** Hours with a break, and "copy to all". Shrinking the hours lists the affected bookings without cancelling them.
- [ ] **AD-04** A shop-wide Navro'z day off removes the day for everyone, with the conflict flow.
- [ ] **AD-05** Bookings: the day, barber and status filters work, and a phone or name search pages 10 at a time. The admin can cancel a booking with a reason.
- [ ] **AD-06** Settings: horizon 14 shows 14 days and enables "📅 Boshqa kun" [P2 calendar]. `abc` is refused. A shared location appears on the address page.
- [ ] **AD-07** Clients: the phone search needs at least 4 digits, returns at most 10, and is audited. Blocking cancels the client's active bookings and notifies them. Blocking a barber or yourself is refused.
- [ ] **AD-08** Stats: a booking at 00:30 local counts on the right day [C6]. Revenue is the sum of final prices. The manual share is shown.
- [ ] **AD-09** No admin callback works for a non-admin, even crafted.
- [ ] **AD-10** [P2] A rating of 2★ reaches the admins privately.

### 5.12 Robustness, privacy, operations
- [ ] **RB-01** C1 blocks the bot: barber actions still succeed and the failure is only logged.
- [ ] **RB-02** `<script>`, `&` and `<b>` in names and comments display literally everywhere.
- [ ] **RB-03** With the server in UTC and `TZ=UTC` set in the environment, a 10:30 booking still shows 10:30, because the bot uses `SHOP_TZ` [C1].
- [ ] **RB-04** `kill -9` during a booking, then restart: no half-written booking and no orphaned `booking_slots` rows (SQL check).
- [ ] **RB-05** Starting a second instance exits with "already running" [A4].
- [ ] **RB-06** Starting with an outdated schema exits with "run python -m app.db.migrate" [#94].
- [ ] **RB-07** The backup file arrives in the env admin chat. Restoring it per the README works [G5].
- [ ] **RB-08** `/delete_me`: active bookings are cancelled, the barber sees "—" instead of the name, and the next `/start` creates a fresh user [H1].
- [ ] **RB-09** A barber's bookings older than yesterday show masked phone numbers. The admin sees them in full [H2].
- [ ] **RB-10** A forced exception in a handler: the callback spinner stops, and the admins get one alert [E4].
- [ ] **RB-11** Every callback is 64 bytes or less (automated).

---

## 6. Risk register

| Risk | Impact | Mitigation | Owner |
|---|---|---|---|
| The begin-mode option does not reach the `begin` event under aiosqlite | P0 | RND-01 spike on Day 2; the two-engine fallback is already specified in ARCH §3.3 | R&D / BE |
| SQLite "database is locked" at peak | P0 | Short write transactions only, no I/O inside them (AST check), `busy_timeout` of 5 s, the claim-then-send outbox, RC-05 | BE |
| Naive datetimes or a wrong zone | P0 | `UTCDateTime`, `SHOP_TZ`, `tzdata`, the startup grid check, RB-03 | BE / QA |
| `booking_slots` cells not freed | P0 | Only `_occupy`/`_release` touch them; the cell count is asserted after every transition | BE |
| Scope: about 245 h across BE, FE and QA | Schedule | The P2 cut line; the work moves in §2.1; the 2-day buffer | PM |
| Bonus farming or abuse | P1 | Verified phones, one stamp per day, walk-in confirmation by the client, the manual share in stats, audit | BE |
| Barbers ignore requests | P1 | Auto-accept by default, admin fallback, the morning digest, late accept | PM / FE |
| Python 3.10 end of life | Ops | 3.12 runtime image, CI on both versions | R&D |
| ZRU-547 hosting | Legal | Owner decision Q9 before go-live; code is host-agnostic | PM |
| Telegram rate limits | P1 | Pacing, `RetryAfter`, 50 claims per tick | FE |

---

## 7. Change control

- A change request is written down: the function, the old and new form, the reason, and the impact on BOT and QA.
- The CTO rules within 2 h.
- The approved change merges together with the stubs, DTOs, the QA-01 parity list and every caller.
- The PM logs it in `docs/CHANGELOG.md`.
- The Judge's rulings in §8 can be reopened only by the CTO, and must cite new evidence.

---

## 8. Judge rulings on the critic's review

**Legend:**
- **ACCEPT:** folded into the spec as written.
- **PARTIAL:** accepted with changes.
- **REJECT:** not adopted, with the reason given.

"Where" points to the binding text.

| # | Sev | Finding | Ruling | Where / what changed |
|---|---|---|---|---|
| A1 | CRIT | Orphan booking is committed after a `ServiceError` | **ACCEPT** | `uow.write_tx` rolls back on any exception. Booking insert and cells sit in one savepoint. The race test checks row counts. ARCH §1.4, §3.4, §5.4, §10. |
| A2 | HIGH | Write lock held across network I/O | **ACCEPT** | Deferred BEGIN for reads and IMMEDIATE only in `write_tx`. `ensure_user` writes only on change. No I/O inside transactions (AST check). Outbox claim → commit → send → mark. ARCH §3.3–3.4, §5.3, §6.5. |
| A3 | MED | Lost update and limit races on Postgres | **ACCEPT** | Atomic balance `UPDATE`; `FOR UPDATE` on the client row in `create_booking` (step 3). ARCH §4.11–4.12. |
| A4 | MED | Duplicate reminders; two instances | **ACCEPT** | `claimed_at` (at most once) and an `fcntl` lock file. ARCH §0.2, §4.13. |
| A5 | LOW | Bonus settings change blocks completion | **ACCEPT** | `bonus_cost` snapshot. If redemption fails, the visit completes at full price with `bonus_failed`. ARCH §4.11. |
| B1 | CRIT | Night bookings expire before the workday | **ACCEPT** | New `compute_expire_at` (cutoff = start − 10). Pending requests are delivered with buttons in the morning summary. ARCH §4.11, §6.2. |
| B2 | HIGH | 5-minute decision window | **ACCEPT** | `min_decision_min` = 15, plus the cross-field rule `min_lead ≥ cutoff + min_decision`. ARCH §4.4. |
| B3 | HIGH | Manual confirmation is the wrong default | **PARTIAL** | Auto-accept is the default. New clients (a setting) and soft-blocked clients still need approval, which also covers D1. The re-ping escalates to admins. |
| B4 | HIGH | Unlinked or unreachable barbers take bookings | **ACCEPT** | Unlinked barbers are not public. An unreachable barber's requests go to the admins at once. Reachability recovers on the barber's next update. |
| B5 | MED | Late tap loses the client | **ACCEPT** | EXPIRED → CONFIRMED within `late_accept_grace_min` (10). |
| B6 | MED | Deactivating without cancelling | **ACCEPT** | Deactivation always cancels future bookings, after a preview. Reassignment is in the v1.1 roadmap. |
| C1 | HIGH | `TZ` env collision | **ACCEPT** | `SHOP_TZ`, a startup grid assertion, and the zone logged. |
| C2 | HIGH | No tzdata | **ACCEPT** | `tzdata` added to the requirements. |
| C3 | MED | TZ read at import time | **ACCEPT** | `get_tz()` is lazy and `bot_token` is optional in Config. conftest sets the env vars first. |
| C4 | MED | Cron jobs are stateless and read only at startup | **ACCEPT** | No cron jobs: marker-guarded daily jobs run inside the tick. |
| C5 | MED | Catch-up burst | **ACCEPT** | Per-kind stale windows in `claim_due`. |
| C6 | LOW | UTC day bucketing | **ACCEPT** | All grouping goes through `day_bounds_utc`; test_stats checks it. |
| C7 | LOW | Wrong weekday examples | **ACCEPT** | Examples fixed (26.10.2026 is dushanba). Labels derive from the calendar. |
| D1 | HIGH | Slot squatting and fake phones | **PARTIAL** | Accepted: verified phone required for bot bookings, limits per phone, new-client approval, blocking cancels bookings. Rejected: the per-barber daily cap on new-client PENDING requests (the verified phone, per-phone limits and admin visibility are enough for one shop). The "unverified number" badge is moot. |
| D2 | HIGH | Stamp farming through manual bookings | **ACCEPT** | One earn per client per local day (DB unique), walk-in stamps only after the client confirms in the bot, the manual share in stats, an audit entry for every manual completion. |
| D3 | HIGH | Walk-ins linked to the wrong account | **ACCEPT** | Link only on a single verified-phone match. A typed phone never overwrites a verified one. |
| D4 | MED | Invite link is a bearer token | **ACCEPT** | Admin approval step and a 24 h TTL. |
| D5 | MED | Forged callbacks read records | **ACCEPT** | `get_booking_for_actor` on all reads; the unchecked `get_booking` is internal and grep-banned in handlers. |
| D6 | LOW | Late-cancel fairness; raw-id referral | **ACCEPT** | Only CONFIRMED cancels count as late. `ref_code` is opaque. No self or same-phone referral. |
| E1 | HIGH | No exit from the FSM | **ACCEPT** | `StateFilter("*")` exits in the first router; `/menu`, `/cancel`; inline ❌ on every prompt. |
| E2 | HIGH | Contact keyboard breaks edit-in-place | **ACCEPT** | New summary message, old keyboard stripped, `msg_id` guard, menu keyboard restored. |
| E3 | HIGH | MemoryStorage | **ACCEPT** | `DbStorage` is the default (Redis optional). `SlotCb` carries barber and service. |
| E4 | MED | Photo edits, "not modified", hanging spinners | **ACCEPT** | `screen.render` and a global error handler. |
| E5 | MED | Families blocked | **PARTIAL** | Back-to-back bookings allowed and `max_active` raised to 3. A "Necha kishi?" (how many people) group booking is v1.1. |
| E6 | MED | Long slot grids | **ACCEPT** | Grouped grid (#5). |
| E7 | MED | Bot in groups | **ACCEPT** | Private-only middleware; the bot leaves groups. |
| E8 | LOW | `tel:` URLs; venue with no location | **ACCEPT** | Phone as text; venue only when lat/lon are set. |
| F1 | HIGH | Walk-ins not recorded | **ACCEPT** | One-tap "⏸ Hozir bandman". |
| F2 | MED | Shops run from one reception phone | **REJECT for v1.0** | The admin fallback covers the urgent case. A reception/admin group (#83) is planned for v1.1, because group chats conflict with the private-only rule and need their own permission design. |
| F3 | LOW | "navbat" may mean a live queue | **DEFER** | Owner question Q10. A live-queue view is a v1.1 candidate. |
| F4 | LOW | Fixed grid wastes gaps | **ACCEPT** | End-of-block candidates in `compute_free_slots`. |
| G1 | HIGH | No admin on first run | **PARTIAL** | `/whoami` and a one-time setup link. Rejected: refusing to start without admins, because the setup link makes it unnecessary. |
| G2 | HIGH | Sticky admin flag | **ACCEPT** | Env admins are computed at runtime. The DB flag is for promoted admins only. Admins section in the bot. |
| G3 | HIGH | Demo seed reaches production | **ACCEPT** | `--demo` flag and a non-empty-DB guard. |
| G4 | HIGH | Relative SQLite path | **ACCEPT** | Path resolved from `PROJECT_ROOT`, with mkdir. |
| G5 | HIGH | No backups | **ACCEPT** | Online backup API daily, 14 kept, integrity check, sent to the env admins. systemd unit and Dockerfile. |
| G6 | MED | Python 3.10 end of life | **PARTIAL** | Code stays 3.10-compatible (the installed toolchain). Production uses 3.12. CI runs both. Install verified on Day 1. |
| G7 | MED | No migrations | **ACCEPT** | Alembic baseline now, a separate migrate step, and the startup schema check. |
| G8 | MED | Enum formatting; `ValidationError` name clash | **ACCEPT** | `StrEnumType` and `.value` everywhere; renamed to `InputError`. |
| H1 | HIGH | ZRU-547 localisation | **PARTIAL** | The code side is accepted: consent screen, `/delete_me`, host-agnostic deploy. The hosting provider is the owner's decision (Q9) before go-live, and it does not block development. |
| H2 | MED | Barbers see too much | **ACCEPT** | Phones masked on bookings older than yesterday. Roles re-read on every update. Admin-only audited phone search. |
| H3 | LOW | Unbounded throttle; text not throttled | **ACCEPT** | TTL buckets covering messages and callbacks. |

**Top-10 status:** all ten are resolved in the spec (items 1–9 in full; item 10 is partial because hosting is the owner's call).

---

## 9. Roadmap (everything not in v1.0)

**v1.1 (next release, about 2 weeks after v1.0):**
- Waitlist [#86] and reschedule [#16].
- Broadcast to segments [#79] and CSV/Excel export with formula escaping [#80].
- Client card notes [#81].
- Full reviews with text and photo, rating on the barber card, and a Google/Yandex/2GIS review link for 4–5★ [#54][#55][#56].
- Birthday greeting and bonus [#40].
- Service categories [#12] and the per-barber price/duration admin UI [#14].
- Barber reminders (evening agenda, 1 h ping) [#32] and personal barber stats [#33].
- Delivery-failure alerts [#39].
- Editable welcome and "About us" text [#61].
- "Ask a human" relay [#63].
- Shadowban [#69].
- Advanced analytics: no-show rate and source report [#78].
- Reception/admin group [#83][F2].
- Uzbek Cyrillic locale [#87].
- Fake Bot API end-to-end tests [#91].
- "Necha kishi?" group booking [E5], reassigning bookings on barber deactivation [B6], live-queue view (if Q10 says yes) [F3].
- Postgres hosting option with `EXCLUDE` as a second guard [RND-03].
- Any P2 items cut at M4.

**Later:**
- `.ics` files [#18] and recurring appointments [#19].
- Configurable reminder offsets [#36].
- Loyalty tiers [#47] and bonus expiry [#49].
- Deposits through Click/Payme with Telegram Payments [#50].
- Gift certificates and memberships [#52], off-peak discounts [#53].
- Role reset on phone change [#72].
- LTV/retention analytics [#78].
- One-way Sheets/Calendar export [#82].
- Payroll [#84].
- A static FAQ [#62].

**Skip:** Telegram Stars tips [#51], channel gate [#64], captcha [#70], inventory [#85], Mini App [#95], multi-tenant SaaS [#96] (the `shop_id` columns are kept).

---

## 10. Open questions for the owner (PM-02, due Day 3)

| # | Question | Default if unanswered |
|---|---|---|
| Q9 | Hosting in Uzbekistan: UzCloud or a local VPS? | Development continues regardless. Go-live is blocked until this is answered. |
| Q10 | Should a live first-come queue sit alongside appointments? | No (appointments only). |
| Q11 | Must a new client's first booking be approved? | Yes. |
| Q12 | Welcome and referral bonus amounts | 1 stamp each. |
| Q1–Q8 | | ARCH §0.4 defaults. All can be changed in Settings. |
