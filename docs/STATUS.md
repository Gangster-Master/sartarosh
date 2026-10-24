# Sartarosh navbat bot: status (v1.0 release candidate)

**Date:** 2026-10-24. **Written by:** the Judge, after the critic, CTO, QA and integration reviews.

**Verdict:** ready for a first deploy with a real bot token. Every HIGH and MEDIUM finding is fixed or
mitigated. The open items are listed under "Known limitations" and in the roadmap.

## 1. What is done

- **Client:** a booking wizard on a single message. The steps are service, barber or "🤝 Farqi yo'q" (any free barber), day, time, comment, contact, then a summary with a bonus toggle.
- **Client, other features:**
  - Back and cancel on every step, and stale-button guards.
  - Rebook "same as last time".
  - My bookings: cancel, withdraw, answer a proposed time, and "Kelaman".
  - Prices, barber cards, address with hours, map links and a venue pin.
  - Stamp or cashback loyalty, welcome bonus, referral, `/delete_me`.
- **Barber:** accept, reject or propose another time on the push message. Today and upcoming queues, done and no-show, staff cancel with a reason, "busy now", time blocking with a conflict preview, and manual walk-in bookings. A morning summary and a nightly digest of unresolved bookings.
- **Admin:**
  - Barbers: invite link plus approval, deactivation with a preview.
  - Services, hours and breaks, days off, bookings, clients (bonus ±, block, reset no-shows).
  - Stats, all business settings, admins, role switch.
- **Ops:**
  - SQLite in WAL mode with Alembic migrations (`0001` baseline and `0002` for `users.deleted_phone_hash`).
  - Single-instance lock, daily online backup, Docker and compose files, systemd notes in the README.
- **Ideas from GitHub projects (the owner's request):** `docs/IDEAS.md` reviews about 20 open-source barbershop and salon bots. Section 12 records the keep or skip decision for each idea.
  - **Adopted in v1.0:** "any barber", barber-first entry, quick days plus a month calendar, the grouped slot grid, edit-in-place screens, back navigation, "propose another time", pending expiry with a fallback to admins, walk-ins, the stamp card, welcome and referral bonuses, reminders, the soft-block for no-shows, and the booking source.
  - **Deferred:** the rest of the ideas are in the roadmap below.
  - **Code reuse:** all code was written from scratch, so `THIRD_PARTY_NOTICES` is not needed.

**Checks after this pass:**

| Check | Result |
|---|---|
| `pytest -q` | **338 passed** (329 before, plus 9 new regression tests in `tests/test_review_fixes.py` and `tests/test_scheduler.py`) |
| `ruff check .` and `ruff format --check` | clean |
| Import of every `app.*` module, `compileall` | OK |
| `python -m app` with no token | exit 2, "BOT_TOKEN is not set" |
| `python -m app` on an unmigrated DB | "schema not at head" |
| `python -m app.db.migrate`, `seed --demo`, `ops.backup` | OK (scratch DB) |
| `python -m app` with a fake token | migrations at head, exit 2, "BOT_TOKEN was rejected" |
| `python -m app` with bad `ADMIN_IDS` or `LOG_LEVEL` | a warning is logged for each bad value |

## 2. Judge rulings: critic findings

| # | Finding | Ruling | What was done |
|---|---|---|---|
| 1 | A late accept gives a client two overlapping bookings | **Fixed** | `confirm_booking` (late-accept path) runs `_late_accept_client_ok`, which checks overlap by client id and phone, and the active limit for bot bookings. If either check fails it raises `TooLateError`. Test: `test_late_accept_refused_when_client_rebooked_same_time`. |
| 2 | `/delete_me` lets a client collect the welcome and referral bonuses again | **Fixed** | New column `users.deleted_phone_hash` (sha256 of the verified phone, migration `0002`). The welcome bonus, the "first visit" check and the referral "already credited" check now cover the user plus any deleted accounts with the same phone. Test: `test_delete_me_does_not_repay_welcome`. |
| 3 | When a proposal expires, the client is told the barber didn't answer | **Fixed** | `booking_expired` checks `awaiting == CLIENT`. In that case the client gets `notify.client_proposal_expired`, the barber gets `notify.outcome.proposal_expired`, and admins get no alert. Both locales are updated. Test added. |
| 4 | A digest button wipes the whole digest | **Fixed** | Digest buttons use `BookingCb(a="dd"/"dn")`. The handler completes the booking or marks the no-show, removes only that row from the keyboard, and shows the result as an alert. Test: `test_digest_button_keeps_the_rest_of_the_list`. |
| 5 | Repeated idempotent actions send duplicate notifications | **Fixed** | `client_my` notifies only if the state before the call allowed the change: the booking was active (cancel or withdraw), the proposal was still open (`pya`/`pno`), or the visit was unanswered (`vn`). |
| 6 | "Any barber" shows the base price | **Fixed** | The summary resolves the price for every eligible barber. If prices differ, it shows "💵 Narx: X dan …" (from the lowest price). The result screen shows the price that was actually booked. |
| 7 | Internal or raw reasons are shown to clients | **Fixed** | Cancellations caused by time off store `busy`/`sick`/`time_off` and never the barber's free text. Deactivation stores `barber_inactive`; the admin's text stays in the audit log. `reason.time_off` and `reason.barber_inactive` were added in uz and ru. |
| 8 | The bot never leaves groups | **Fixed** | `allowed_updates(dp)` adds `my_chat_member`. Test added. |
| 9 | Messages can exceed 4096 characters | **Fixed** | `screen.clip()` cuts at a line break, so HTML stays balanced. It is applied in `screen.render`, `Notifier.send` and `Notifier.edit`, which covers all lists. Test added. |
| 10 | A bad map URL breaks the address page | **Fixed** | `update_setting` checks `map_*_url` with `validation.is_web_url`. `show_address` uses the same check and renders the page again without map buttons if Telegram still rejects one. Test added. |
| 11 | Collusion on manual bookings farms bonuses | **Mitigated** | Referral credit is never paid for manual bookings. The welcome bonus is now once per phone (see #2). Earnings stay capped at one per client per day, and every manual completion is audited. A per-barber report of manual completions is on the roadmap. |
| 12 | Duplicate "barber" entry in the back stack | **Fixed** | `_choose_barber` pops the stack when booking from the barber card. |
| 13 | The decision message is overwritten after accept | **Fixed** | `booking_confirmed(b, skip=(chat, msg))` leaves the message that the handler just redrew. |
| 14 | The staff-cancel prompt keeps a working ❌ | **Fixed** | `strip_keyboard` runs on the prompt before the card is shown. |
| 15 | The walk-in summary shows the base price | **Fixed** | The summary uses `catalog.resolve_service(barber_id, service_id)`. |
| 16 | Lost updates on PostgreSQL | **Fixed (for v1.1 PG)** | `_load_locked` uses `SELECT … FOR UPDATE` on PostgreSQL in every state transition. SQLite is already serialised by `BEGIN IMMEDIATE`. |
| 17 | The time-off UI ignores `barbers_can_block_time` | **Fixed (partly)** | "Busy now" and "block time" check the setting at entry and show `err.forbidden.setting`. The menu buttons are still visible (cosmetic, roadmap). |
| 18 | `on_role` crashes on an old message | **Fixed** | Falls back to the callback as the target and opens the hub as a new message. |
| 19 | A huge bonus delta causes `OverflowError` | **Fixed** | The cap is \|delta\| ≤ 10⁹, enforced in both the handler and `adjust_bonus`. Test added. |

## 3. Judge rulings: CTO findings

| # | Finding | Ruling | What was done |
|---|---|---|---|
| 1 | Docker can't write to `./data` (uid 10001) | **Fixed (docs)** | README Docker section: `mkdir -p data && sudo chown 10001:10001 data`, plus the symptom if this is skipped. |
| 2 | Restore over stale WAL files | **Fixed** | README restore: `rm -f data/bot.db-wal data/bot.db-shm` and `chown` for Docker. The `app.ops.backup` restore hint says the same. |
| 3 | Manual backup and seed on Docker | **Fixed** | The README shows the `docker compose exec bot …` commands. |
| 4 | No upgrade section | **Fixed** | New README section "## Update" for Docker and local installs, with "back up first". |
| 5 | `systemctl` is mentioned but there is no unit | **Fixed** | A sample systemd unit is in the README. |
| 6 | Lock file frozen on 3.10 | **Partly fixed** | `async-timeout` and `tomli` now have `python_version < "3.11"` markers, so the 3.12 image skips them. Re-freezing on 3.12 is still to do, because Python 3.12 is not available on this machine. |
| 7 | Typos in `ADMIN_IDS` or `LOG_LEVEL` are dropped silently | **Fixed** | A warning is logged at startup (`Config.invalid_admin_ids`). |
| 8 | `RuntimeError` loses the traceback | **Fixed** | Only "schema not at head" is a one-line error. Any other `RuntimeError` is logged with `log.exception`. |
| 9 | The setup link is a credential | **Documented** | The README warns about it and recommends `ADMIN_IDS` in production. A 24 h expiry is on the roadmap. |
| 10 | No healthcheck | **Accepted** | `restart: unless-stopped` covers it, and the README says so. |
| 11 | The backup sent to admins contains phone numbers | **Documented** | Privacy note in the README. An on/off setting is on the roadmap. |

**QA and integration:** their own fixes were reviewed and kept. These were the night-time "were you there?" message, the stale block "Yes", the `too_fast` notice, alembic `path_separator`, the clean exit on an invalid token, and logging with `--migrate`.

## 4. Known limitations

- **Not yet tested with a real bot token:** the manual end-to-end pass (QA-10) still has to be done in Telegram. Everything else ran against a fake Telegram session.
- **Python 3.12 not tested:** the tests ran on Python 3.10.12 only, and the Docker image is 3.12. The Docker image was not built here because there was no access to the Docker socket.
- **Single instance:** one shop on SQLite. PostgreSQL is the v1.1 hosting option; the row locks are already in place.
- **A second proposal can't happen:** once the barber proposes a time, nothing returns the booking to the barber. The "max 2 proposals" rule is therefore effectively 1.
- **Spec error:** the docs use `Asia/Kathmandu` (+05:45) as a zone that should fail the 15-minute grid check, but +05:45 passes it. The test uses a +00:20 zone instead.
- **After `/delete_me`:** the per-phone daily request counter starts again, because old bookings lose their phone. Bonuses are protected (see #2).
- **Minor edge cases:**
  - After a no-show flip, the client cannot earn another stamp that same day.
  - "New clients" in stats also counts staff accounts.
  - At service level, a bonus can be reserved on a free (0 so'm) service. The bot UI hides that option.
- **Staged walk-ins:** a barber and a friend can still earn one stamp a day through staged walk-ins (audited; see #11).

## 5. Next steps (roadmap)

**Before go-live (owner, about 1 hour):**
1. Create the bot in @BotFather, then fill `.env` (`BOT_TOKEN`, `ADMIN_IDS`).
2. Deploy: `mkdir -p data && sudo chown 10001:10001 data && docker compose up -d --build`.
3. Go through README "First start": shop info, services, invite barbers, hours.
4. Run the QA-10 manual pass with two phones (client and barber). Re-freeze `requirements.lock` in `python:3.12-slim`.

**v1.1 (about 2 weeks, from PLAN.md §9 plus this review):**
- Waitlist and reschedule.
- Broadcast to segments, and CSV/Excel export.
- Full reviews and rating, with a link to Google, Yandex or 2GIS reviews.
- Birthday bonus.
- Service categories and an admin UI for per-barber prices.
- Barber evening agenda and personal stats.
- Delivery-failure alerts.
- Editable welcome and "About us" texts.
- "Ask a human" relay, and a reception group.
- Uzbek Cyrillic locale.
- PostgreSQL hosting with `EXCLUDE`.
- Items from this review:
  - A per-barber report and alert for manual completions (#11).
  - A 24 h expiry for the setup link.
  - A setting to turn off sending the backup to Telegram.
  - Hide the block and busy menu buttons when `barbers_can_block_time` is off.
  - Let the barber make a second proposal.

**Later:** `.ics` files and recurring bookings, loyalty tiers and bonus expiry, Click/Payme deposits, gift certificates, LTV analytics, Sheets/Calendar export, payroll.

## 6. Web App (Telegram Mini App)

**Done.** An aiohttp server runs in the bot process (`WEBAPP_ENABLED`, `WEBAPP_HOST`,
`WEBAPP_PORT`, `WEBAPP_URL`, `WEBAPP_AUTH_MAX_AGE[_WRITE]`, `WEBAPP_TRUST_PROXY`). It serves
`app/webapp/static`: plain HTML/CSS/ES modules with no build step, Telegram theme variables,
MainButton/BackButton and haptics, and Uzbek Latin text. It also serves a JSON API under `/api`.

- **Auth:** every call carries the initData. It is checked with HMAC-SHA256 using
  secret = HMAC("WebAppData", token), compared in constant time, with an age check, a 60 s
  future-skew limit, a 4 KB cap and duplicate keys rejected. The user is mapped to the `users`
  row the same way as `/start`. Blocked users, consent and roles are enforced.
- **Client screens:** home, the booking wizard (including "Farqi yo'q", the phone step via
  `requestContact` and the bonus option), my bookings with the bot's cancel policy, bonus with
  the referral link, and barbers with prices.
- **Barber screens:** a queue with Qabul qilish / Rad etish / Bajarildi / Kelmadi.
- **Writes:** all go through `app.services` under a per-booking lock and an idempotency key.
  Notifications go through the bot's `Notifier`, so the barber gets the same Accept/Reject
  message.
- **Bot:** sets the chat menu button and adds a "🌐 Ilovani ochish" inline button on the client
  hub.
- **Tests:** 423 pass in total. About 80 of them cover the Mini App: auth, API, QA and the judge
  fixes. An API smoke test and a headless-Chrome end-to-end run against the real backend also
  pass. `node --check` passes on all 15 JS modules.

**Judge rulings on the review findings:**

| # | Finding | Ruling |
|---|---|---|
| 1 | Phone fallback pointed to a bot button that isn't there | **Fixed.** New `POST /api/me/request-phone` sends the bot's `contact_request` keyboard before the app opens the chat. `webapp_contact` saves the reply. |
| 2 | Write session expired after 1 h (barber dead end) | **Fixed.** `WEBAPP_AUTH_MAX_AGE_WRITE` now defaults to 86400, the same as reads, since the initData is the same bearer token either way. A session-token exchange is still deferred. README explains the trade-off. |
| 3 | Users who only open the app may never get messages | **Fixed.** `/api/me` returns `allows_write_to_bot`. Before submitting a booking the app calls `requestWriteAccess` (6.9+), and Telegram answers at once if access was already allowed. If the user refuses, a confirm warns that no confirmations or reminders will arrive. |
| 4 | All users share one IP behind a proxy | **Fixed in docs.** README says to set `WEBAPP_TRUST_PROXY=true` for nginx, Caddy and cloudflared. The default stays `false`. |
| 5 | Leftmost X-Forwarded-For can be spoofed; limiter clear-all | **Fixed.** The rightmost entry is used, and the limiter evicts the oldest key instead of clearing all. |
| 6 | Late-accept of an expired booking is not possible in the panel | **Fixed.** The queue includes EXPIRED bookings that are still late-acceptable. |
| 7 | A malformed `#/…%E0` hash hangs boot | **Fixed.** The decode is wrapped in try/catch. |
| 8 | Huge ids cause a 500 | **Fixed.** Ids above 2^53 return 400 `invalid`. |
| 9 | Idempotency key ignored the bonus toggle | **Fixed.** The key is computed at submit time over the full payload. |
| 10 | Barber buttons depend on the phone clock | **Fixed.** The clock offset comes from the HTTP `Date` header, and the buttons unlock in place when the time arrives. |
| 11 | Menu button label is always "Ilova" | **Won't fix (cosmetic).** The UI is Uzbek-only. |
| 12 | After consent, no reply menu or commands; panel scope not documented | **Fixed.** Consent in the app sends the `menu_for` keyboard and sets per-chat commands. README lists what stays in the bot. |
| 13 | `WEBAPP_HOST` default 127.0.0.1 instead of 0.0.0.0 | **Accepted deviation.** It is safer behind a proxy, and docker-compose sets 0.0.0.0. |

**Limitations:**
- **Not tested in real Telegram:** not yet run in the real clients (Android, iOS, Desktop).
- **Port 8080:** on the dev machine it is taken by another `node` process. Pick another
  `WEBAPP_PORT` there.
- **Barber actions not in the app:** propose time, staff cancel, time blocks and admin screens
  stay in the bot.
- **Session length:** a Mini App open for more than 24 h shows "Sessiya eskirdi" and must be
  reopened. A server session token is planned for v1.1.
- **JS size:** about 80 KB unminified, against a 60 KB budget.
- **Bot token:** it was pasted in chat, so revoke it in BotFather and set the new one in `.env`.
