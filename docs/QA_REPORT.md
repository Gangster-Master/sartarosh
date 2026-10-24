# Sartarosh: QA report (whole product)

**Date:** 2026-10-24. **Baseline:** commit `8f8384a` (pre-QA checkpoint), 423 tests.
**Result:** 491 automated tests pass, `ruff check .` is clean, `node --check` passes on every
file in `app/webapp/static`. Nothing is committed yet. The running bot still has the old code
loaded: **none of this is live until the bot is restarted.** No migration is needed (the new
outbox kind `thx` goes into the existing string column).

## 1. Scope

- The Telegram bot (client, barber and admin flows), the Mini App (`app/webapp`, API and
  static JS), the services layer, the scheduler and the outbox, the data layer, and deployment
  (`run.py`, Dockerfile, README).
- Extra scrutiny on the newest changes: public barber ratings, hiding barbers without working
  hours, the admin one-tap hours presets, and the whole Mini App.

## 2. Method

1. A risk-based plan (`docs/QA_PLAN.md`) split the product into seven hunting areas: client,
   barber, admin, Mini App, time and scheduler, security and abuse, and a simulated real-world
   month.
2. Seven hunters wrote a failing repro test for every suspected bug, driving the real stack
   (`tests/bot_harness.py` runs the real aiogram dispatcher against a fake Telegram session;
   `tests/webapp_harness.py` runs the real aiohttp app). No real Telegram API, no real DB.
3. A triage pass ran all 70 repro tests, merged duplicates and ruled every finding against
   REQUIREMENTS, ARCHITECTURE and QA_PLAN: 57 raw findings became **42 confirmed**,
   12 duplicates and 3 rejected (2 by design, 1 invalid).
4. A fix pass fixed the 42, then the repro tests moved into the suite as `tests/test_qa_*.py`.
5. Final sign-off (this document): the whole diff was reviewed line by line, one weak fix was
   strengthened (real_world-1: the barber now gets an hour to answer the 23:00 digest before
   auto-completion), and pytest, ruff and `node --check` were re-run.

## 3. Summary

| severity | confirmed | fixed | partial |
|---|---|---|---|
| critical | 2 | 2 | 0 |
| high | 6 | 6 | 1 (barber-3, see §5) |
| medium | 21 | 21 | 0 |
| low | 13 | 13 | 0 |
| **total** | **42** | **42** | **1** |

Rejected: client-7 (BY_DESIGN: `/delete_me` cancels all active bookings, per REQUIREMENTS C20),
time_scheduler-11 (BY_DESIGN: the shop cannot close after midnight, QA_PLAN §7), webapp-4
(INVALID: only `/` is a documented Mini App route).

## 4. Findings by area

Test files are in `tests/`. "Fixed" means the repro test fails on the old code and passes now.

### Mini App

| id | severity | finding | status | test |
|---|---|---|---|---|
| webapp-1 | critical | Booking wizard and barbers screen did not load: `ui.js` lacked the `fmtRating` export | Fixed | `test_qa_webapp::test_every_named_import_in_static_js_is_exported` |
| client-8 (+webapp-2) | medium | A crafted booking with `service_id=null` gave a 0 so'm booking while the shop sells services | Fixed | `test_qa_client_webapp`, `test_qa_webapp` |
| webapp-3 | low | A crafted start time off the slot grid was accepted (one booking blocked two slots) | Fixed | `test_qa_webapp::test_crafted_off_step_start_is_refused` |
| webapp-5 | low | A 429 "throttled" left screens on skeletons with no retry | Fixed | none (JS only) |

### Client

| id | severity | finding | status | test |
|---|---|---|---|---|
| client-2 (+security_abuse-2) | critical | A walk-in the client denied (or never confirmed) could be rated and counted in the public rating | Fixed | `test_qa_client_rating`, `test_qa_security_ratings` |
| client-3 (+barber-5) | high | Walk-ins got review and return pushes, even after the client answered "Yo'q" (REQUIREMENTS B9) | Fixed | `test_qa_client_walkin`, `test_qa_client_rating`, `test_qa_barber` |
| client-4 (+security_abuse-3) | high | Auto-completed visits accepted crafted ratings; the rating survived a no-show flip | Fixed | `test_qa_client_rating`, `test_qa_security_ratings` |
| client-1 (+webapp-6) | high | A barber with no working hours was still offered in rebook paths, the barber card and the Mini App, ending at an empty day step | Fixed | `test_qa_client_hourless` (4 tests) |
| client-5 (+security_abuse-4) | medium | The low-rating alert to admins was re-sent on every repeated star tap | Fixed | `test_qa_client_rating`, `test_qa_security_ratings` |
| client-6 | medium | A second, opposite answer to "edingizmi?" showed a false confirmation | Fixed | `test_qa_client_walkin` |
| client-9 | low | Leaving the wizard at the phone step left only the "send phone" keyboard | Fixed | `test_qa_client_wizard` |
| client-10 (+time_scheduler-7) | low | The address page showed today as open on a shop-wide day off | Fixed | `test_qa_client_address`, `test_qa_scheduler_edges` |

### Barber

| id | severity | finding | status | test |
|---|---|---|---|---|
| barber-3 (+barber-4, security_abuse-1) | high | A barber could rate his own visit and earn stamps (self-booking, or a walk-in with his own phone) | Fixed (partial, §5) | `test_qa_barber`, `test_qa_security_ratings` |
| barber-2 (+time_scheduler-6, real_world-6) | medium | A late accept of an expired request skipped the slot, barber and client checks | Fixed | `test_qa_barber`, `test_qa_scheduler_edges`, `test_qa_real_world` |
| barber-1 | medium | The late accept was unreachable in the bot (no button after expiry) | Fixed | `test_qa_barber::test_late_accept_is_reachable_in_bot_within_grace` |
| barber-6 | medium | Proposing another time could give the client two overlapping bookings | Fixed | `test_qa_barber::test_proposal_cannot_give_client_overlapping_bookings` |
| barber-7 | low | Admins got the "barber blocked the bot" alert on every failed send | Fixed | `test_qa_barber::test_unreachable_barber_alert_sent_once` |
| barber-8 (+time_scheduler-10) | low | A barber with an empty day never got the morning summary | Fixed | `test_qa_barber`, `test_qa_scheduler_edges` |

### Admin

| id | severity | finding | status | test |
|---|---|---|---|---|
| admin-1 | medium | Copying hours from a day that is now off wiped the barber's whole week | Fixed | `test_qa_admin` (2 tests) |
| admin-2 | medium | A stale break button of another barber saved hours to the wrong barber (D-4) | Fixed | `test_qa_admin` |
| admin-3 | medium | An off-grid typed range (e.g. 10:10) was accepted, then every break choice failed | Fixed | `test_qa_admin` |
| admin-4 | medium | Booking search by name did not find Cyrillic names | Fixed | `test_qa_admin` |
| admin-5 | medium | A deleted user's leftover balance blocked switching the loyalty mode forever | Fixed | `test_qa_admin` |
| admin-6 | medium | Saving a barber's services dropped a temporarily inactive service and its price | Fixed | `test_qa_admin` |
| admin-7 | low | Blocked users could become staff | Fixed | `test_qa_admin` (2 tests) |
| admin-8 | low | A stale demote confirmation messaged a user who was no longer admin | Fixed | `test_qa_admin` |
| admin-9 | low | Latitude/longitude accepted `nan` | Fixed | `test_qa_admin` |
| admin-10 | low | "No-shows reset" wording next to the unchanged lifetime count | Fixed (wording) | none |

### Time and scheduler

| id | severity | finding | status | test |
|---|---|---|---|---|
| real_world-1 | high | Morning visits were auto-completed before the 23:00 digest asked the barber | Fixed | `test_qa_real_world`, `test_scheduler`, `test_no_show_softblock` |
| time_scheduler-8 | high | One long Telegram `RetryAfter` stalled the whole scheduler tick (D-7) | Fixed | `test_qa_scheduler_edges` |
| time_scheduler-1 (+-2, -3) | medium | Turning off reminders, reviews or return pushes did not stop already queued ones | Fixed | `test_qa_scheduler_outbox` (3 tests) |
| time_scheduler-4 | medium | Blocked clients still got return, review and visit-check pushes | Fixed | `test_qa_scheduler_outbox` |
| time_scheduler-9 | medium | A backup of 45 MB or more was silently not sent (D-6) | Fixed (admin alert) | `test_qa_scheduler_edges` |
| real_world-2 | medium | The auto-completion "thanks" push went out at about 03:30 | Fixed (sent by day) | `test_qa_real_world` |
| real_world-5 | medium | A request whose decision message failed to send outside hours expired unseen | Fixed | `test_qa_real_world` |
| time_scheduler-5 | low | The return reminder said a deactivated barber "is waiting" | Fixed | `test_qa_scheduler_edges` |
| real_world-7 | low | The nightly digest of a deactivated barber went only to him | Fixed (goes to admins) | `test_qa_real_world` |
| real_world-8 | low | The morning summary counted unanswered requests as bookings | Fixed (⏳ mark) | `test_qa_real_world` |

### Security, bonus and lifecycle

| id | severity | finding | status | test |
|---|---|---|---|---|
| security_abuse-5 | medium | An unconfirmed walk-in counted as the first visit, so welcome and referral bonuses were lost | Fixed | `test_qa_security_bonus` (3 tests) |
| security_abuse-6 | medium | `/delete_me` plus rejoining with the same phone cleared the no-show soft-block | Fixed | `test_qa_security_bonus` |
| real_world-3 | medium | A barber's `/delete_me` orphaned his clients' future bookings silently | Fixed | `test_qa_real_world` |
| real_world-4 | medium | `run.py --migrate` migrated before taking `bot.lock`; the README update step migrated the live DB under the old code | Fixed | `test_qa_real_world` |

## 5. Deferred and partial

- **barber-3 (partial):** a barber can still book himself as a client (a product decision).
  The abuse is closed: he cannot rate that visit, it earns no stamps, and a walk-in is never
  linked to the barber or to the staff member typing it.
- **time_scheduler-4 (optional part):** blocking a user does not cancel his queued pushes at
  once; they are dropped when they come due.
- **Docs formatting:** `ruff format --check` flags code blocks in `docs/ARCHITECTURE.md` and
  `docs/WEBAPP.md`. This predates the QA round.

## 6. Residual risks

- **Not live yet.** The Python fixes need a restart of the running bot. The static Mini App
  files are served `no-cache` straight from this folder, so the JS fixes (webapp-1, webapp-5)
  are already live. Until the restart, the old API sends no rating fields, so the Mini App
  barbers screen shows "⭐ Hali baho yo'q" for every barber. Restart soon.
- **Hidden barbers.** A barber with no working hours is now hidden everywhere for clients,
  including the rebook buttons. After the restart, check every real barber has hours.
- **Auto-completion is later.** A visit now auto-completes no earlier than 00:00 (23:00 digest
  plus one hour to answer), and otherwise at `end + auto_complete_after_hours`. The client's
  "thanks + stamp" message for an auto-completed visit arrives the next morning (10:00).
- **Ratings shrink.** The public average now counts only real visits (barber-marked done, or
  a walk-in the client confirmed). Ratings left on auto-completed or denied visits stop
  counting, so a shown average can change after the restart.
- **Strict start times.** Bookings must start on the barber's slot grid or right after a busy
  block. If an admin changes a barber's slot step while a client has the slot list open, that
  client's tap is refused with "slot unavailable" and he picks again.
- **Long Telegram flood waits.** Outbox pushes are rescheduled; a direct message (decision,
  alert) that hits a wait over 5 s is dropped, not retried. A decision message that reaches
  nobody is re-pinged through the outbox.
- **Never tested for real:** the Mini App in a real Telegram WebView on iOS and Android, the
  Docker image (Python 3.12), and migrating a DB that holds real rows. The new
  `py_lower` SQLite function is registered per connection; Postgres uses `lower()`.

## 7. Manual checklist on real phones

Use real Telegram on at least one Android and one iPhone. Ideally use a test bot token and a
scratch `DATA_DIR`; if you test on the live bot, use your own accounts and cancel test
bookings afterwards. Restart the bot with the new code first.

### Client: bot

- [ ] `/start` as a new user: language, consent, phone share. Press a menu command at the
      phone step: the main menu comes back (not only the "send phone" button).
- [ ] Book: service, barber (rating ⭐ shown on rated barbers), day, time, confirm. The barber
      gets the request; after he accepts you get the confirmation.
- [ ] A barber with no working hours does not appear anywhere (list, card, rebook).
- [ ] "Mening navbatlarim": cancel, then rebook from a past visit.
- [ ] Rate a visit the barber marked done: stars once; tapping again gives no second admin
      alert for a low rating.
- [ ] Walk-in check: the barber adds you as a walk-in with your phone and marks it done. You
      get only "edingizmi?". Answer "Ha": rating and return pushes follow later. Tap "Yo'q" on
      an older copy: "already answered" appears.
- [ ] Address page on a shop-wide day off: today shows closed.

### Client: Mini App

- [ ] Open the Mini App from the menu button: home loads, no endless skeleton.
- [ ] Barbers screen: each barber shows `⭐4.8 (12)` or "⭐ Hali baho yo'q".
- [ ] Booking wizard: barber rows show duration and rating; a full booking end to end.
- [ ] Open a booking link for a barber who is off or has no hours: the toast says he is not
      taking bookings.
- [ ] Tap quickly many times to get "throttled": an error box with a retry button appears.

### Barber: bot

- [ ] Accept, reject and propose another time on a request. A proposed time never overlaps
      another booking of the same client.
- [ ] Let a request expire: the expired message and the card show "✅ Baribir qabul qilish"
      during the grace period; it works and refuses if the slot closed meanwhile.
- [ ] Add a walk-in, mark done and no-show; flip an auto-completed visit to no-show.
- [ ] Morning summary arrives even with no bookings; pending requests are marked ⏳.
- [ ] 23:00 digest lists unmarked visits (including morning ones); the visits auto-close
      only after midnight if not marked.
- [ ] Try rating your own visit: refused.

### Admin: bot

- [ ] Working hours: one-tap presets, typed range `10:10-20:00` is refused, breaks, copy to
      all days. Copy from a day that is off: "stale" message, the week is untouched.
- [ ] Barbers: services save keeps a temporarily disabled service; deactivate and reactivate;
      reactivating a barber whose user is blocked is refused.
- [ ] Bookings search by a Cyrillic client name finds it.
- [ ] Admins: promote and demote; a second tap on an old demote confirmation does nothing.
- [ ] Settings: latitude `nan` is refused; turning reminders or reviews off stops pushes.
- [ ] Client profile: the soft-block reset button wording; block and unblock a client.
- [ ] Backups arrive at 03:30 (or the "too big" alert, if the DB is over 45 MB).
