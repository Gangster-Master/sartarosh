# Sartarosh: risk-based QA plan (whole product)

**Date:** 2026-10-24. **Author:** the QA lead. **Baseline:** 423 automated tests pass
(`.venv/bin/python -m pytest -q`).
**Scope:** the Telegram bot, the Mini App (`app/webapp`), the services layer, the scheduler, the
data layer and migrations, and deployment (Docker and README).
**Inputs:** ARCHITECTURE.md, WEBAPP.md, PLAN.md §5 (acceptance checklist), STATUS.md and
README.md, plus a code skim focused on the recent changes:

- public barber ratings (`BarberDTO.rating_avg` and `rating_count`, in `_common.barber_dtos`);
- hiding barbers that have no working hours (`_common.has_hours`);
- one-tap presets for admin hours (`admin_hours.py`);
- the whole of `app/webapp/`.

> **Safety rules for anyone running this plan:** a real bot is running from this folder with
> real users. Do not start, stop or kill `run.py`. Do not bind port 8081 or 8080. Do not read
> `.env`, and do not touch `data/`. Never call the real Telegram API. All automated tests use
> the in-memory or scratch DB, the `FakeSession` in `tests/bot_harness.py`, and
> `tests/webapp_harness.py`. Do manual passes on a **separate test bot token and a scratch
> `DATA_DIR`**.

---

## 1. Risk model

Severity follows PLAN.md §4:

- **P0:** double booking, orphan rows, data loss, a crash loop, a permission bypass (including
  reads), a wrong time shown, or a bonus double-spend.
- **P1:** a broken user story.
- **P2:** cosmetic issues.

**Likelihood** is High, Med or Low. It is driven by:

- how new the code is (the Mini App and the recent changes are the newest);
- how many entry points reach the same state (bot, Mini App and scheduler);
- whether the existing tests reach the real failure mode. The fake Telegram session never
  returns an error.

| Rank | Area | Why it is risky |
|---|---|---|
| 1 | Bot and Mini App acting on the same booking | Two UIs plus the scheduler change one row. Webapp locks are in-process asyncio locks the bot does not take. Notifications are sent outside the transaction. |
| 2 | Telegram failure modes | `FakeSession` never raises. Forbidden, RetryAfter, "message can't be edited" and timeouts are untested on every path. |
| 3 | Visibility rules for hourless barbers | `has_hours()` is applied in list queries only. `is_public()` and `BarberDTO.is_public` ignore it (see §9, D-1). |
| 4 | Public rating integrity | A rating is visible to everyone now, but it is still accepted on manual walk-ins the client denied, and it survives a no-show flip (see §9, D-2 and D-3). |
| 5 | Time edges (Tashkent, UTC+5, no DST) | The local midnight is 19:00 UTC. Day counters, horizon, lead time, the 23:00 digest and the 03:30 backup all straddle it. |
| 6 | Scheduler under load or outage | A single `tick` runs with `max_instances=1`. A long `RetryAfter` sleep inside `Notifier._api` stalls expiry and the outbox. |
| 7 | Mini App auth and IDOR | initData is a bearer token for 24 h. Reads and writes are role-checked per request. |
| 8 | Migrations on real data | Only an empty DB is upgraded in tests. There is no test of 0001 → 0002 with rows in it. |
| 9 | Deployment docs | `.env.example` lacks the `WEBAPP_*` keys. The Docker image (3.12) has never been built or tested. |

---

## 2. Roles and the permission matrix

| Role | How it is obtained | Must be able to | Must NOT be able to |
|---|---|---|---|
| Guest (no consent) | first `/start` or first app open | pick a language, give consent | book, see prices behind the gate, use any API except `/me` and `/consent` |
| Client | consent given | book, cancel or withdraw, answer proposals, Kelaman, rate, use bonus and referral, `/delete_me` | read or act on others' bookings, use barber or admin callbacks or API |
| Barber (linked, active) | invite link plus admin approval | queue, accept, reject, propose, done, no-show, staff cancel, busy, blocks (if the setting allows), walk-ins | touch another barber's bookings, admin sections, their own hours (admin only) |
| Barber (pending link) | opened an invite, not yet approved | sees "Tasdiq kutilmoqda" | anything a barber can do |
| Barber (deactivated) | admin deactivation | acts as a client | barber callbacks, barber API (roles are re-read on every update) |
| Promoted admin | DB `is_admin` | every admin section | demote an env admin |
| Env admin | `ADMIN_IDS` | everything, and receives backups | be demoted from the bot |
| Blocked user | admin "block" | sees the `blocked` text at most once per 10 min; the API returns 403 `user_blocked` | any read or write |
| Deleted user | `/delete_me` | the next `/start` creates a fresh user | regain the old bonus, welcome or referral (phone hash), or show their name to barbers |
| User who blocked the bot | Telegram side | — | stall notifications, or crash flows that notify them |

**Covered:**

- `test_permissions.py`, `test_users_admin.py` (env vs promoted, block, delete) and
  `test_bot_e2e.py` (crafted callbacks, blocked user).
- `test_webapp_auth.py` (consent gate, blocked, non-barber) and `test_webapp_qa.py` (the
  matrix for the barber API).

**Gaps** are listed per area in §8.

---

## 3. Booking state machine

```
              create (bot / app / any-barber)
                     │
   auto-accept & ok  ▼                 reject
 ┌──────────────── PENDING(awaiting=BARBER) ──────────────► REJECTED
 │                   │  │ propose                      ▲
 │                   │  ▼                              │ expire
 │                   │ PENDING(awaiting=CLIENT) ──decline──► CANCELLED_BY_CLIENT (not late)
 │                   │  │ accept proposal               │
 │      confirm      ▼  ▼                               │ expire (proposal_expired)
 └─────────────► CONFIRMED ◄── late accept ── EXPIRED ◄─┘
                     │   (≤ late_accept_grace, slot free, client ok)
   cancel (client) ──┼──► CANCELLED_BY_CLIENT (late if <2 h and CONFIRMED)
   cancel (staff)  ──┼──► CANCELLED_BY_STAFF / ADMIN (reason required)
   done (≥ start−30) ┼──► COMPLETED ──(auto, flip ≤ 24 h)──► NO_SHOW (stamp reversed)
   auto at end+12 h ─┘        │ manual: visit_confirmed yes/no
   no-show (≥ start+15) ─────► NO_SHOW
```

**Invariants to check after every transition** (several are already asserted in
`test_booking_state_machine::cells_invariant_active_only`):

1. `booking_slots` rows exist only for PENDING and CONFIRMED bookings, and cover exactly
   `[start, end + buffer)` on the 15-minute grid.
2. Pending and reminder rows in `scheduled_notifications` are cancelled when a booking leaves
   the active states.
3. Bonus: `bonus_balance` equals the recomputed sum of the ledger
   (`test_bonus::recompute_matches_cached`). A reserved bonus is released on cancel, no-show
   or expiry.
4. A repeated action is idempotent: it returns the unchanged DTO and sends **no** second
   notification.

**Transitions not covered end to end:**

- EXPIRED → CONFIRMED through the **Mini App** after the client has started another booking
  through the **bot**.
- An auto-completed walk-in (`MANUAL`) flipped to NO_SHOW after the client answered "Ha".
- PENDING(awaiting=CLIENT) when the client is deleted (`/delete_me`) while a proposal is open.
- A transition hit by bot, app and scheduler in the same second (see §8 Concurrency).

---

## 4. Rules: bonus, referral and rating

| Rule | Covered by | Gap |
|---|---|---|
| Stamps 1 per visit, plus the welcome stamp, at most 1 earn per local day | `test_bonus` (`five_visits…`, `welcome…`, `same_day_earns_once`) | The day boundary at 19:00 UTC: two visits at 23:30 and 00:30 local are on different days and must earn twice. A visit at 18:59 and one at 19:01 UTC fall on different local days. |
| Redeem only if `available` covers it; snapshot `bonus_cost` | `reserved_and_insufficient`, `snapshot_cost_survives…` | Two bonus bookings created in parallel, one from the bot and one from the app. Bonus on a 0-price service (known limitation, not enforced in the service). |
| Percent and cashback modes | `percent_reward`, `cashback_quote_and_earn` | Cashback rounding on odd prices (for example 33 333 × 5 %). The Mini App `/api/booking/quote` matches the bot summary in both modes. |
| Referral: credited on the referee's first real visit; not for auto-completed visits, manual visits, self or same phone | `referral_credit_rules`, `referral_not_on_auto_completed` | Referral through `?startapp=ref_<code>` in the Mini App (only `/start ref_` is tested through the web auth). A referrer who is deleted or blocked before the payout. |
| Welcome bonus once per phone, even after `/delete_me` | `test_review_fixes::delete_me_does_not_repay_welcome` | The same through the Mini App sign-up path. |
| Manual adjust: comment required, never below 0, \|delta\| ≤ 10⁹ | `adjust_requires_comment…`, `huge_bonus_delta_rejected` | — |
| **Rating 1..5: own COMPLETED booking, once** | `test_public_rating` (avg/count, badge) | **Rating on a MANUAL booking whose visit the client denied (`visit_confirmed=False`), see D-2. The rating stays after an auto-complete is flipped to NO_SHOW, see D-3. A crafted `RateCb` on an auto-completed booking (no prompt sent) is still accepted. Ratings by deleted or blocked users still count. The rating badge is missing from the Mini App barber list (parity). Rounding: 4.25 → 4.2 or 4.3 (banker's `round`). The badge with exactly one rating. The bot flow of `client_feedback.on_rate`: low rating goes to admins once, double tap.** |

---

## 5. Scheduler jobs

One `tick` runs every N seconds and does: expire, auto-complete, outbox, daily jobs (morning,
nightly, backup) and FSM purge.

| Job | Covered | Gaps |
|---|---|---|
| Pending expiry (`expire_due_pending`) | `test_pending_expiry`, `test_scheduler::expire_tick_tells_client` | Expiry while the barber is tapping Accept at the same instant (EX-01 vs BD-06). Expiry of a proposal through the app. Every decision message is edited when Telegram says "message can't be edited" (older than 48 h). |
| Re-ping at 15 min to the barber and admins | `test_notifications::rping_*` | Delivery when the barber blocked the bot: goes to admins at once and marks the barber unreachable in the tick path. |
| R24 and R2 reminders, Kelaman suppresses R2 | `test_notifications`, `test_scheduler` | Kelaman sent from the app (not exposed there, so check parity). A reminder for a booking moved by a proposal (old R2 cancelled, new R2 created). |
| Stale windows on restart (C5) | `stale_cutoffs_restart_catch_up`, `r24_stale_after_3h` | — |
| Return and review reminders, 10:00–21:00 clamp | `completion_schedules_ret_and_rev`, `clamp_to_daytime` | The clamp at exactly 21:00 and 09:59 local. The return reminder for a client who rebooked through the app. |
| Auto-complete at end + 12 h | `auto_complete_tick` | A walk-in auto-completed at night asks "edingizmi?" after 10:00 (`auto_completed_manual_asks_next_morning`, covered). An auto-complete across local midnight with day stats. |
| Morning summary, once a day with grace | `morning_summary_once_and_not_after_grace` | `daily_summary_time` changed **earlier** after today's run, or **later** before it ran. A crash after the marker is written: the summary is lost by design, so document it. Pending requests appear with buttons [B1]. |
| Nightly digest at 23:00 | `nightly_digest_lists_unresolved`, `digest_button_keeps_the_rest_of_the_list` | A digest longer than 4096 characters or with more than 100 buttons (the inline keyboard limit) on a busy day. Digest buttons tapped after the auto-complete. |
| Backup at 03:30 | `test_backup::backup_and_keep` (the function) | **The tick path is untested:** the marker, skipping a non-file DB, `alert_admins` on failure, the 45 MB send cut-off with no alert when skipped, `send_document_to_env_admins` with no env admins, and `backup_time` parsing. |
| FSM purge after 7 days | `test_queries::db_fsm_storage_survives_restart` (storage) | Purge removes only stale rows. A user inside a wizard at day 7 gets `session_expired` rather than a crash. |
| Tick robustness | — | One failing step does not stop the others (`try/except` per step). **A `TelegramRetryAfter(retry_after=300)` inside `_api` blocks the whole tick** (expiry is late for everyone). Outbox: a client who blocked the bot is marked failed, not retried forever. More than 50 due rows drain over several ticks. |

---

## 6. Time edges (Asia/Tashkent, UTC+5, no DST)

| Edge | Covered | Gap |
|---|---|---|
| `day_bounds_utc`, weekday labels, grid check | `test_clock` | — |
| Lead time rounded up to the grid, past slots, half-open end | `test_slots_pure`, `test_slots_db::wrapper_today_respects_lead` | Booking at 18:55 UTC (23:55 local) for "Ertaga": the labels shift exactly at 19:00 UTC. A Mini App `GET /api/booking/days` at 19:00:00 UTC returns the new `today`. |
| Horizon (today .. today + N − 1) | `sunday_and_horizon`, `past_day_and_horizon` | A request for the first day past the horizon via a crafted `start_at` in the API (the server must refuse it; the UI only hides it). The horizon reduced while bookings exist beyond it. |
| `max_per_day` per local day | `max_per_day_counts_cancelled` | A request at 23:59 and 00:01 local must count on different days. |
| A shop closing near midnight | — | Hours of 10:00–23:59. A 30-minute service at 23:30 (end 00:00, which belongs to the next local day). 00:00 as an end time is not allowed (`start < end`), so document that the shop cannot close after midnight. |
| Stats bucketing | `test_stats::local_day_bucketing_and_revenue` | — |
| Server in UTC with `TZ=UTC` (RB-03) | grid assertion at startup | One test with `TZ=America/New_York` in the env, asserting the times shown. |
| Mini App clock offset | the judge fix (HTTP `Date`) | The JS is not unit tested: a phone clock 1 h ahead must not unlock Done or No-show early. The server still enforces this (`TooEarlyError`), so only the UI is at risk. |

---

## 7. Telegram quirks

`tests/bot_harness.FakeSession` returns success for every call, so **none of these error paths
are exercised**. Add a fault-injecting session (`H.fail_next(method, exc)`) as the top-priority
harness work.

| Quirk | Code path | Status |
|---|---|---|
| Stale buttons (an old message, the wizard moved on) | `views.stale`, `msg_id` guard | Covered (`test_bot_flows`, `forged_callbacks_rejected`, crawl). |
| Double taps on ✅, Accept and Done | idempotent services plus `debounce_window` | Covered for create and services. **Gap:** a double tap on the admin hours `std` and `cpbg` presets (guarded by `_AlreadySet`). Only a sequential stale tap is tested. |
| "message is not modified" | `screen._not_modified` | Not injected. |
| "message to edit not found" or "can't be edited" (older than 48 h, deleted by the user) | `screen.render` fallback, `Notifier.edit` | Not injected. Decision messages older than 48 h are common for bookings made days ahead. |
| Photo to text edit (barber card with a photo, then back) | `screen.render` | Covered by CL-02 in the crawl. Fails only with a real Telegram, so do it manually. |
| 4096-character limit | `screen.clip` | `clip_keeps_lines_whole` is covered. **Gap:** the admin bookings list, the digest and the morning summary with 100 bookings stay under 4096 and valid HTML after clipping (an `<b>` cut in the middle). The 100-button keyboard limit. |
| A user who blocked the bot | `TelegramForbiddenError` → barber unreachable, or a client log | **Gap:** no test raises it. Check booking creation, staff cancel, expiry, reminders, Mini App `post_booking` (inline notify) and `request-phone`. |
| RetryAfter | `_api` sleeps and retries once | **Gap:** a second RetryAfter is raised to the caller. Inline in `post_booking`, it hangs the HTTP request for `retry_after` seconds. |
| Group chats | private-only middleware, the bot leaves | Covered (`group_messages_ignored_and_bot_leaves`, `allowed_updates…`). |
| Callback data ≤ 64 bytes | `all_keyboard_callbacks_fit` | Covered. Add the admin hours `rng` values with 6-digit barber ids. |
| A contact from another user | `contact_step_and_foreign_contact` | Covered. |
| Bot restart in the middle of the wizard | DbStorage | Covered (`db_fsm_storage_survives_restart`). |

---

## 8. Coverage and gaps by area

Each gap has an ID, a priority and the kind of test to write: **U** for a service unit test,
**B** for the bot harness, **W** for the webapp harness, **M** for a manual test on a test bot.

### 8.1 Client (bot)

**Covered:** onboarding (language, consent, ru skips the picker), the full wizard,
"any barber", rebook, back and cancel on each step, session expiry, the menu leaving the
wizard, name editing, contact and foreign contact, slot taken meanwhile, withdraw and cancel,
proposals, cancel policy, limits, the per-phone limit, soft-block, `/delete_me` (service), and
blocked users.

**Gaps:**

- **C-01 P1 B:** "🔁 Avvalgidek" or the barber card "✂️ Shu ustaga yozilish" for a barber whose
  hours were all removed. `BarberDTO.is_public` ignores `has_hours`, so the wizard jumps to the
  day step with no days (D-1).
- **C-02 P1 B:** a deep link or old message opens the card of a barber without hours. It shows
  "book this" and services instead of `info.barber_unavailable`.
- **C-03 P1 B:** the bot rating flow (`client_feedback.on_rate`):
  - a double tap;
  - a low rating alerts the admins exactly once;
  - a crafted `RateCb` for another client's booking is refused;
  - a crafted `RateCb` for an auto-completed booking (D-3);
  - a crafted `RateCb` for a denied walk-in (D-2).
- **C-04 P1 B:** the bot flow for `/delete_me`: confirm, cancel the active bookings, notify the
  barber with "—", then `/start` creates a fresh user. Today only the service is tested.
- **C-05 P1 B:** the deleted user's open proposal or pending booking. The barber's decision
  message must not crash on a negative or anonymised `tg_id`.
- **C-06 P2 B:** referral: `/start ref_<own code>` and `ref_<unknown>` are ignored, and
  `ref_` sent by an existing user does not overwrite the referrer.
- **C-07 P1 B:** the walk-in "edingizmi?" Ha/Yo'q buttons tapped twice, and after the 24 h
  window.
- **C-08 P2 B:** the address page with the location set and with no map URLs, and today's
  "ochiq/yopiq" (open/closed) at 23:59 and 00:00.
- **C-09 P1 B:** `/menu` and `/cancel` from **every** FSM state (CL-15). The crawl does not
  assert this for the name, comment and contact states.
- **C-10 P2 B:** the month calendar ("📅 Boshqa kun") with horizon 14: tapping a past day,
  tapping a day past the horizon, and a month change.

### 8.2 Barber (bot)

**Covered:** accept, reject (free text), propose, no-show and done timing, staff cancel
reason, busy now, blocks and conflicts, the walk-in linking rules, digest buttons, masked
phones, crafted callbacks from another barber, and reachability recovery.

**Gaps:**

- **BR-01 P1 B:** with `barbers_can_block_time=False`, the menu buttons are still visible
  (known). Assert the alert on every entry point: busy, block and the day block.
- **BR-02 P1 B:** a barber deactivated while they have the queue open. The next tap on Done or
  Accept must be refused, because roles are re-read.
- **BR-03 P1 B:** propose time when the proposed slot is taken between listing and tapping.
  The 8-slot list at the end of the day and in the break.
- **BR-04 P1 B:** walk-ins:
  - at a time inside the barber's own block;
  - outside hours (lead time ignored, past refused, covered);
  - a phone matching a **deleted** user's phone hash.
- **BR-05 P1 B+W:** the barber accepts in the bot **and** in the Mini App within the same
  second. One CONFIRMED, one client message, and both UIs show the final state.
- **BR-06 P1 B:** the barber has no hours set (new barber). The barber menu, `/today` and
  "Kelgusi kunlar" show a helpful note, not empty lists or errors.
- **BR-07 P2 B:** the morning summary for a barber with 40+ bookings (clip and buttons).
- **BR-08 P1 U:** the no-show flip of an auto-completed **manual** booking after the client
  said "Ha". The stamp is reversed exactly once and the rating handled (D-3).

### 8.3 Admin (bot)

**Covered:**

- the setup link;
- invites, approval, TTL and reuse;
- service validation;
- settings parsing, including the cross-field rule;
- client search, limit and audit;
- block and its guard rails;
- deactivation preview;
- hours: shrink, `copy_to_all` and validation;
- the new-barber presets (`std`, `rng`, `brk`, `nobr`, `off`, `cpb`);
- stats bucketing;
- the section crawl.

**Gaps:**

- **A-01 P1 B:** **admin hours presets:**
  - `rng` from a stale message for another barber or weekday must give STALE (the guard
    exists but is untested);
  - `brk`/`nobr` from a stale message while the FSM holds **another barber** saves to the
    FSM's barber, not the button's `bid` (D-4);
  - a typed range of 15:00–20:00 followed by the ☕ 13:00–14:00 preset gives a validation
    alert and keeps the state;
  - a double tap on `std` while the first is in flight;
  - `cpbg` from an inactive or hourless source barber;
  - `cp` (copy to all) on a barber with bookings outside the new hours lists them;
  - turning off the **last** working day hides the barber from clients. Show an extra
    warning when they have future bookings (product decision).
- **A-02 P1 B:** role switch (`on_role`) on an old message (STATUS #18 fix) has no test.
- **A-03 P1 B:** promote and demote through the bot UI:
  - the demoted admin loses the admin menu on the next tap;
  - an env admin cannot be demoted;
  - an admin cannot demote themselves into zero admins.
- **A-04 P1 B:** blocking a client who is also a barber, or the last admin, is refused (the
  service is covered, the UI is not).
- **A-05 P1 B:** a shop-wide day off (Navro'z) through the UI, with cancel and keep, notifies
  every affected client once.
- **A-06 P2 B:** admin bookings search paging (10 per page), and a search string with `%` and
  `_` (SQL LIKE wildcards).
- **A-07 P2 U:** "New clients" in stats counts staff (a known limitation). Lock the decision
  with a test.
- **A-08 P1 B:** a setting change takes effect without a restart:
  - `daily_summary_time`;
  - `booking_horizon_days`;
  - `min_lead_min`;
  - `reviews_enabled=false` stops new REVIEW rows.
- **A-09 P1 U:** deactivating a barber keeps their ratings (reviews) but hides them. On
  reactivation, the rating comes back.

### 8.4 Mini App (`app/webapp`)

**Covered:**

- initData (signature, tamper, expiry, future skew, size, duplicates; checked against a
  reference implementation);
- consent gate, blocked users, the barber-only API, JSON content type;
- per-IP and per-user rate limits, rightmost X-Forwarded-For, limiter eviction;
- the wizard reads, quote, create, idempotency replay, concurrent creates, cancel and withdraw
  policy, proposal answer, foreign booking 403;
- the barber queue, accept, reject, done and no-show, the late-acceptable EXPIRED item;
- the photo endpoint, huge id → 400, the request-phone keyboard, `allows_write_to_bot`, consent
  syncing the bot menu, static files, headers.

**Gaps:**

- **W-01 P0 W:** IDOR sweep. Run every `/api/bookings/{id}*` and barber route with another
  client's id, another barber's id, a deleted booking id, `0`, `-1`, a float, and
  `"1 OR 1=1"`. Also check that a **plain admin** can read a booking through
  `GET /api/bookings/{id}` (allowed by code) but cannot write to it.
- **W-02 P1 W:** `post_booking` when `Notifier.booking_created` raises (Forbidden or timeout).
  The booking is committed, so the response must be 201, and a retry with the same key must
  replay the result. `@_guard` makes the notifier safe, but `store.put` runs after it, so a
  slow or cancelled notify loses the replay record (D-8).
- **W-03 P1 W:** the idempotency store is in memory. After a process restart, a resubmit with
  the same key must not create a second booking (the slot cells guard it). Assert the error
  code the UI gets.
- **W-04 P1 W:** a booking with the `barber_id` of a barber **without hours**, inactive, or
  unlinked gives a clean 4xx. `/api/booking/days?barber_id=` for that barber returns an empty
  list, not a 500.
- **W-05 P1 W:** `start_at` with a timezone offset other than +05:00, a naive value, a time off
  the grid, a past time, a time past the horizon, or a leap-second string.
- **W-06 P1 W:** `?startapp=ref_<code>` through `start_param`: the referrer is stored only for
  a new user, and self-referral is ignored.
- **W-07 P1 W:** a deleted user opening the app with the old initData gets a fresh user, not
  the anonymised row. A blocked user's photo request still works (it is public by design).
- **W-08 P1 W:** a user whose role changes while the app is open:
  - a barber deactivated mid-session gets 403 on the next barber call;
  - a client blocked mid-session gets 403 on the next write.
- **W-09 P2 W:** parity gaps: there is no rating, no Kelaman (`client_ack`), no walk-in
  confirmation and no rating badge in the app. Either add them or record them as
  bot-only in WEBAPP.md. The barber list API omits `rating_avg`.
- **W-10 P1 W:** the comment field: 1000+ characters, control characters, RTL override, emoji
  ZWJ sequences, and `<script>` is returned as text (covered).
- **W-11 P1 W:** request bodies:
  - `Content-Length` larger than the limit;
  - an invalid JSON body;
  - a JSON array instead of an object;
  - `use_bonus: "true"` as a string (must be treated as false).
- **W-12 P2 M:** the front end has no automated JS tests. Run a manual or headless pass on
  Android, iOS, Desktop and Web for:
  - BackButton and MainButton states;
  - the theme switching to dark;
  - a `#/…` deep link;
  - `requestWriteAccess` declined;
  - the clock offset;
  - the "Sessiya eskirdi" screen after 24 h.
- **W-13 P1 W:** `post_cancel` and `post_proposal` while the scheduler expires the same booking
  at the same moment. Exactly one notification goes out.

### 8.5 Time and scheduler

**Covered:** see §5 and §6 (the clock, slots, expiry formula, reminders and catch-up, morning
once with grace, digest, auto-complete, stats bucketing).

**Gaps:**

- **T-01 P0 U:** the local day boundary at 19:00 UTC for:
  - `max_per_day`;
  - stamp once per day;
  - "Bugun" labels;
  - the `today` of the barber queue;
  - the phone-mask cutoff (yesterday).
- **T-02 P1 U:** a shop open 10:00–23:59 with a 30-minute service at 23:30. The slot is
  offered, the end of 00:00 is on the next local day, and stats and reminders still work.
- **T-03 P1 U:** a crafted `start_at` on the first day past the horizon, and at exactly
  `now + min_lead` (allowed), and one second earlier (refused).
- **T-04 P1 U:** the backup tick:
  - the marker is set once;
  - failure → `alert_admins`;
  - more than 45 MB → not sent, and **no one is told** (D-6);
  - a non-file DB is skipped;
  - `backup_time` of `"25:99"` falls back to the default.
- **T-05 P1 U:** `daily_summary_time` moved earlier or later on the same day. Each job runs at
  most once a day.
- **T-06 P1 U:** the tick when the notifier raises RetryAfter(300). Expiry for other bookings
  must not wait 5 minutes. At least, document the risk and cap the sleep.
- **T-07 P1 U:** outbox delivery to a client who blocked the bot: the row is marked failed and
  not retried in a loop. A barber who blocked the bot is marked unreachable from the tick
  path.
- **T-08 P2 U:** the return reminder at exactly 21:00 and 09:59 local (clamp edges).
- **T-09 P1 U:** a proposal moves the booking. The old R24 and R2 rows are cancelled and new
  ones are created for the new start.

### 8.6 Security and abuse

**Covered:**

- crafted callbacks (barber-to-barber, client-to-admin);
- `get_booking_for_actor` on reads;
- initData verification, rate limits and throttling;
- a blocked user;
- HTML escaping in bot texts and no `innerHTML` in the app;
- the setup link used once;
- invite TTL and approval;
- the per-phone limits;
- the bonus delta cap;
- the "no Telegram I/O inside transactions" AST check.

**Gaps:**

- **S-01 P0 U+W:** rating integrity, now public:
  - a rating on a denied walk-in (D-2);
  - a rating that survives a no-show flip (D-3);
  - a rating on an auto-completed booking via a crafted callback;
  - one client rating every visit (decide whether to weight or cap per client);
  - staged walk-ins farming 5★.
- **S-02 P0 B:** a full sweep of every `AdminCb` section and action sent by:
  - a client;
  - a barber;
  - a demoted admin;
  - a deleted admin.

  Today the coverage is a sample (`admin_callback_refused_for_client`, `test_bot_crawl`).
- **S-03 P0 B:** every `BookingCb` action by a client on their own booking that only staff may
  perform (`done`, `ns`, `dd`, `dn`, accept).
- **S-04 P1 U:** a `phone_search` input of fewer than 4 digits, or wildcard characters, cannot
  enumerate clients.
- **S-05 P1 W:** a replay of a write initData from another user's device inside 24 h cannot be
  prevented (by design). Check that a captured initData cannot be used after the user is
  blocked or deleted (it cannot, because the user is re-read).
- **S-06 P1 U:** promote and demote cannot escalate:
  - a promoted admin cannot promote while being demoted at the same time;
  - an invite link token cannot be used to become an admin;
  - a barber pending link cannot use barber callbacks.
- **S-07 P1 B:** the 10-minute window for the blocked notice, and `too_fast` throttling also
  applies to callbacks spammed by one user (LM-05).
- **S-08 P1 W:** the Mini App headers: CSP forbids inline script, `X-Frame-Options` or
  `frame-ancestors` allows Telegram (web.telegram.org) but nothing else, and there is no
  directory listing or path traversal under `/static` (`/static/../.env` must return 404).
- **S-09 P2 U:** names with Unicode homoglyphs or zero-width characters (impersonating
  "Admin"), and phones in `+998`, `998` and `8` forms all normalise to one value for the
  per-phone limits.
- **S-10 P1 U:** a backup sent to Telegram contains phones (documented). Check that it goes
  only to **env** admins, never to promoted admins.

### 8.7 Real world (concurrency, data, deploy, ops)

**Covered:**

- the race test with exactly one winner and no orphan cells;
- serialised concurrent web creates;
- concurrent cancel notifying once;
- write-tx rollback;
- migrations to head with no diff;
- the schema check;
- the single-instance lock (code);
- backup and retention.

**Gaps:**

- **R-01 P0 U:** `alembic upgrade` from **0001 with data** to 0002 (users, bookings, reviews,
  settings), and a downgrade. Run it against a copy made with `sqlite3 .backup` of a
  **scratch** DB that mimics production (never `data/`).
- **R-02 P0 U:** backup → restore round trip:
  - restore a backup file;
  - remove the WAL and SHM files;
  - `assert_schema_current`;
  - `PRAGMA integrity_check`;
  - row counts are equal.
- **R-03 P0 U:** bot, Mini App and scheduler on one booking at the same instant:
  - accept vs expire;
  - cancel vs done;
  - proposal answer vs proposal expiry.

  The final state must be legal, there must be no orphan cells, and one notification goes per
  recipient. Use `asyncio.gather` on a file-backed SQLite in WAL mode, not `:memory:`, so that
  real locking and `busy_timeout` are used.
- **R-04 P1 U:** a burst of 50 bookings in parallel against a file DB while the outbox sends
  50 reminders. There must be no "database is locked" error, and p95 latency must stay under
  1 s (RC-05).
- **R-05 P1 M:** a `kill -9` during booking creation leaves no half-written rows (RB-04).
  Run it only on a scratch instance with a test token on another port.
- **R-06 P1 M:** build the Docker image (`python:3.12-slim`) and start it with a test token:
  - `migrate` runs;
  - `/healthz` answers on 8080 inside the container;
  - the volume is owned by uid 10001;
  - the bot exits cleanly on a bad token.

  It has never been built (STATUS §4).
- **R-07 P1 docs:** README and `.env.example` accuracy:
  - `.env.example` has none of `WEBAPP_ENABLED`, `WEBAPP_URL`, `WEBAPP_PORT`,
    `WEBAPP_TRUST_PROXY` and the other `WEBAPP_*` keys (D-5);
  - the README restore steps;
  - the systemd unit uses `python -m app.bot.main`, while the Dockerfile CMD uses
    `app.bot.main` and `run.py`. All three must stay equivalent;
  - the README must say that compose publishes 8080 while the dev machine uses another port.
- **R-08 P1 M:** re-freeze `requirements.lock` on 3.12 and run the whole suite on 3.12 (CI on
  both versions per PLAN).
- **R-09 P1 U:** a second instance started from the same `DATA_DIR` exits with "already
  running" (RB-05), tested with a scratch `DATA_DIR`. The Mini App port being busy logs the
  problem and the bot keeps polling (`port_in_use_raises_oserror` covers the raise, not the
  degrade path).
- **R-10 P1 M:** the QA-10 manual pass with a **test** bot token and two phones covers all of
  PLAN.md §5. This is the first real Telegram run, for both the bot and the Mini App on
  Android, iOS and Desktop.
- **R-11 P2 U:** the data growth bounds:
  - `scheduled_notifications` and `audit` keep growing (is there a purge?);
  - FSM purge;
  - the backups directory keeps 14 files.
- **R-12 P1 U:** a corrupt stored setting (covered). A missing `tzdata` package or a bad
  `SHOP_TZ` value fails at startup with a clear message.

---

## 9. Suspected defects found during the review (verify first)

| ID | Sev | Where | Finding |
|---|---|---|---|
| D-1 | P1 | `_common.is_public`, `BarberDTO.is_public`, `client_booking.start_booking` (rebook/barber_first), `client_info.on_barber_card`, `slots.get_available_days` | `has_hours()` filters only the list queries. A barber with no hours is still `is_public=True`. Rebook and "book this barber" therefore jump to a day step with no days. The card shows "book" and services. Expected: treated as unavailable, as in the lists. |
| D-2 | P0 (public data) | `reviews.submit_rating`, `notifications.schedule_after_completion` | A MANUAL booking with `visit_confirmed=False` (the client said they were not there) is still COMPLETED. It gets a REVIEW prompt and can be rated, which feeds the public `rating_avg`. |
| D-3 | P1 | `bookings.mark_no_show` (flip) | The flip cancels a pending REVIEW notification but does not delete an existing `Review`. Ratings for visits that turned out to be no-shows stay public. `submit_rating` also accepts auto-completed bookings, which never get a prompt, through a crafted `RateCb`. |
| D-4 | P2 | `admin_hours.on_admin_hours` (`nobr`/`brk`) | Only the FSM state is checked. The button's `bid` is not compared with `data["barber_id"]`, so a stale break button from barber X's screen saves the break for barber Y if Y's flow is open. |
| D-5 | P2 | `.env.example` | No `WEBAPP_*` keys, although the README tells operators to set them. |
| D-6 | P2 | `Ticker._backup` | Backups of 45 MB or more are silently not sent. There is no admin alert, so the operator thinks off-site backups still arrive. |
| D-7 | P1 | `Notifier._api` + `post_booking` | A RetryAfter makes the code sleep while holding the semaphore. Inside the HTTP request and the single scheduler tick, a long flood wait stalls bookings and expiry for everyone. |
| D-8 | P2 | `api.post_booking` | `store.put(...)` runs after `booking_created`. `@_guard` swallows exceptions, so a 500 is unlikely. Two problems remain. First, a RetryAfter delays the 201 response. Second, a `CancelledError` (a `BaseException`, which the guard does not catch) between the commit and `store.put` loses the idempotency record. A retry then gets `slot_taken` for the client's own booking. |

---

## 10. Execution order

1. **Harness work (1 day):** add fault injection to `FakeSession` (Forbidden, RetryAfter,
   BadRequest "not modified", "not found" and "can't be edited", timeouts). Add a file-backed
   WAL SQLite fixture for concurrency tests.
2. **P0 gaps:** D-2, D-3 and S-01 (rating integrity), W-01, S-02 and S-03 (permission
   sweeps), T-01 (the day boundary), R-01, R-02 and R-03.
3. **P1 gaps** from the recent changes: D-1 with C-01, C-02, W-04 and BR-06; A-01 (hours
   presets); W-02 and W-03 (idempotency after a failure).
4. Everything else marked P1. Then the P2 items.
5. **Manual QA-10** on a test bot with a scratch DB (R-10, R-06, W-12).
6. **Exit criteria:**
   - no open P0;
   - the P1 items are fixed or accepted by the owner in writing;
   - the suite is green on 3.10 and 3.12;
   - the manual checklist (PLAN.md §5) is ticked on two real devices.
