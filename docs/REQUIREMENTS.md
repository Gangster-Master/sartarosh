# Sartarosh Navbat Bot: requirements (final, v1.0 MVP)

**Status:** FINAL, issued by the Judge on 2026-10-24. This version replaces the earlier draft. It folds in the Judge's rulings on the critic review (PLAN.md §8) and the GitHub features adopted from IDEAS.md §12. Where this document and ARCHITECTURE.md disagree on a technical detail, ARCHITECTURE.md wins. Where they disagree on product behaviour, this document wins and the conflict is raised to the CTO.

**Product:** a booking and queue system for barbershops that runs only inside a Telegram bot. There is no web app and no Mini App.

**Language:**
- Uzbek, Latin script, is the default.
- Russian is the second locale [P2].
- Uzbek Cyrillic comes in v1.1.
- All strings live in locale files keyed by message id.

**Time:** the shop timezone is set by `SHOP_TZ` and defaults to Asia/Tashkent (UTC+5, no DST). Times are stored in UTC and shown in shop time.

**Scope:** one barbershop (single tenant). The data model carries `shop_id` so the product can become multi-tenant later.

**Tags:**
- **[P2]**: part of v1.0, but behind the cut line. The PM may move it to v1.1 at gate M4 (PLAN.md §1).
- **[#n]**: an adopted GitHub/market idea from IDEAS.md.
- **[Cx]**: a critic finding that was accepted.

---

## 1. Goals and non-goals

### Goals (v1.0)
1. A returning client books in at most 6 taps: service, barber, day, slot, skip comment, confirm. "🔁 Avvalgidek" (rebook as last time) needs 3 taps [#15].
2. Bookings reach the barber instantly. With auto-accept they are confirmed at once. Otherwise the barber decides with one tap, can propose another time [#21], and the admins act as a fallback [#23].
3. Two clients can never hold the same slot. This is enforced in the database.
4. No-shows go down through 24h/2h reminders, the cancellation policy, a soft-block for repeat no-shows, and a verified phone for every bot booking.
5. Clients come back through the stamp card, a welcome bonus, the "time for a haircut" reminder [#37], and referral [P2].
6. The owner manages barbers, services, hours, days off, settings, clients and admins from inside the bot.

### Non-goals (v1.0)
- No web frontend and no Mini App [#95].
- No online payments or deposits [#50]; clients pay at the shop.
- No branches or multi-tenant setup [#96].
- No SMS.
- No AI answers [#62].
- No channel-subscription gate [#64].

---

## 2. Roles

| Role | How assigned | Capabilities |
|---|---|---|
| **Client** | Anyone who presses `/start` in a private chat | Book, rebook, view, cancel or withdraw own bookings; answer proposals and reminders; see bonus and referral; delete own data. |
| **Barber** | The admin creates the barber and sends an invite link `/start inv_<token>` (valid 24 h). The barber opens it and **an admin approves** the link [C-D4]. | See own queue; accept, reject or propose; done and no-show; busy-now; block time (if allowed); manual walk-in bookings. |
| **Admin** | Env admins (`ADMIN_IDS`, cannot be demoted in the bot), admins promoted in the bot, or the first-run setup link [C-G1] | Everything a barber can do, for any barber, plus all admin sections. |

- One account may hold several roles. The menu then shows "🔄 Rol" (role switch).
- `/whoami` shows the user's Telegram id and roles.
- Roles are re-checked on every update. Removing a role takes effect immediately [C-G2][C-H2].

---

## 3. Core entities

The full schema is in ARCHITECTURE.md §3. Summary:
- **User:** Telegram id, name, verified phone, language, consent version, promoted-admin flag, blocked flag, no-show and late-cancel counters, bonus balance (a cache of the ledger), referral code, referrer, acquisition source.
- **Barber:** display name, photo, bio, active flag, linked user, step (15/30/45/60), buffer (0/15/30) [#28], auto-accept (default **on**), invite token and pending link, reachable flag.
- **Service:** name, duration (a multiple of 15 minutes, at most 240), price in so'm, active flag. Preset combos such as "Soch + soqol" are ordinary services [#13]. Per-barber price and duration overrides exist in the schema; the admin UI for them is v1.1 [#14].
- **Working hours:** per barber and weekday, with an optional break.
- **Time off:** per barber or shop-wide (holidays), on the 15-minute grid.
- **Booking:** snapshots of the service, duration, price, buffer and bonus cost; status; who the booking is waiting on; proposal fields; source (bot or manual); expiry; acknowledgement; auto-completion; visit confirmation.
- **Booking slots:** one 15-minute cell row per active booking, which is the double-booking guard.
- **Notifications outbox, bonus ledger, reviews [P2], FSM state, settings, audit log.**

### Booking statuses
```
PENDING(awaiting barber) ──acc──> CONFIRMED ──done──> COMPLETED ──(auto-completed, ≤24h)──> NO_SHOW
   │  ├─prop─> PENDING(awaiting client) ──yes──> CONFIRMED
   │  │                                  └─no───> CANCELLED_BY_CLIENT (not late)
   │  ├─rej──> REJECTED                  CONFIRMED ──ns──> NO_SHOW
   │  └─time─> EXPIRED ──acc within 10 min grace──> CONFIRMED
   ├─client withdraws / cancels ──> CANCELLED_BY_CLIENT
   └─staff / day off / block user ─> CANCELLED_BY_BARBER
```
PENDING and CONFIRMED hold the slot. Every other status frees it.

---

## 4. User stories

### 4.1 Client

| ID | Story | Acceptance criteria |
|---|---|---|
| C0 | Onboarding | First `/start`: pick a language (skipped when Telegram's language is uz or ru), then a one-screen privacy notice with "✅ Roziman" (I agree) [#73]. Nothing else works until the client accepts. When the notice version changes, the client is asked again. |
| C1 | Hub | Reply keyboard: `✂️ Navbatga yozilish`, `📋 Mening navbatlarim`, `🎁 Bonuslarim`, `💈 Ustalar va narxlar`, `📍 Manzil va aloqa`. The inline hub message is edited in place [#7]. It shows "🔁 Avvalgidek yozilish (Barber, Service)" after the client's first completed visit [#15]. The payloads `ref_<code>` [P2] and `src_<tag>` are stored on first contact. |
| C2 | Pick a service | Services with price and duration ("Soch olish — 60 000 so'm · 30 daq"). Skipped when the shop has no services. |
| C3 | Pick a barber, or "Farqi yo'q" | "🤝 Farqi yo'q" (any barber) [#1], then the public barbers offering the chosen service. "ℹ️" opens a barber card with photo and bio [#57]. A barber card can also start a booking ("✂️ Shu ustaga yozilish") [#2]. Unlinked or inactive barbers are never shown [C-B4]. |
| C4 | Pick a day | Quick buttons Bugun / Ertaga / Indinga / `Pa 29.10` up to the horizon [#4]. A day with no free time shows "· band" and an alert. "Bugun" is hidden when today has no slots. "📅 Boshqa kun" opens a month calendar when the horizon is over 7 days [#3][P2]. |
| C5 | Pick a time | The free slots only, grouped as 🌅 Ertalab / ☀️ Kunduzi / 🌙 Kechqurun, 4 per row [#5][C-E6]. Every step has ⬅️ Orqaga (back, earlier choices kept) [#8] and ❌ Bekor qilish (cancel) [#9]. |
| C6 | Comment (optional) | "Izoh qoldirasizmi?" with "➡️ Izohsiz davom etish" (continue without a comment) [#11]. Up to 300 characters. |
| C7 | Verified phone | The first booking requires `📱 Raqamni yuborish` (Telegram contact). Only the client's own contact is accepted. Typed numbers are **not** accepted for bot bookings [C-D1]. After that, the phone is never asked again. |
| C8 | Confirm | The summary shows barber, service, `26-oktabr, dushanba, 10:30`, price, name, phone and comment. Buttons: `✅ Tasdiqlash`, `✏️ Ismni o'zgartirish`, `🎁 Bonusdan foydalanish` (when possible, with the new price shown), ⬅️ and ❌. Availability is re-checked at this moment. |
| C9 | Result | Auto-confirmed: "✅ Navbatingiz tasdiqlandi" with the address and a map pin. Pending: "So'rovingiz yuborildi, tasdiqlanishini kuting" with "↩️ So'rovni qaytarib olish" (withdraw request) [#17]. |
| C10 | Decision notifications | Accepted: confirmation, address and pin. Rejected: an apology, the reason and `🔁 Boshqa vaqt tanlash` (pick another time). Proposal: "Ali 11:00 ni taklif qilmoqda" with `✅ Qabul qilish / ❌ To'g'ri kelmaydi` [#21]. Expired: an apology and a rebook button. |
| C11 | My bookings | Upcoming PENDING and CONFIRMED bookings and the last 5 past ones, with statuses. Cancel on CONFIRMED (with a confirm dialog and a late warning). Withdraw on PENDING. Proposal answer buttons when a proposal is waiting. |
| C12 | Cancel | Free until 2 h before start. Later it is still allowed, but recorded as a **late cancel**, and only for CONFIRMED bookings [C-D6]. The barber is notified. |
| C13 | Reminders | 24 h before (only if booked more than 24 h ahead), with `✅ Kelaman` (I'll come) and `❌ Bekor qilish`. 2 h before as information only, skipped if the client already pressed Kelaman [#35]. |
| C14 | Bonus page | Stamps shown as `✂️✂️✂️▫️▫️ 3/5` with "6-chi soch olish bepul" (the 6th haircut is free) [#43], or a cashback balance [P2][#42]. Also the last transactions and the referral link with a share button [P2][#46]. |
| C15 | Info | Price list [#58]. Address, phone, weekly hours generated from the schedule with "Bugun: 10:00–20:00 (ochiq)" [#60], a venue pin (when set) and Yandex/Google/2GIS buttons [#59]. |
| C16 | Rebook | "🔁 Avvalgidek" on the hub, and a rebook button on thank-you, rejection, expiry and cancellation messages. These go straight to the day step [#15]. |
| C17 | Return reminder | N weeks (default 4) after the last completed visit, if nothing is booked: "Soch olish vaqti keldi ✂️" with a rebook button [#37]. Sent between 10:00 and 21:00 only. |
| C18 | Rate the visit [P2] | 2 h after a completed visit: ⭐1–5. A rating of 3 or less goes privately to the admins [#54]. |
| C19 | Manual visit check | After a walk-in that is linked to the client is completed: "Bugun soat 15:00 da Ali'da edingizmi?" (were you at Ali's at 15:00 today?) [Ha]/[Yo'q]. The stamp is credited only on "Ha" [C-D2]. |
| C20 | Delete my data | `/delete_me` with a confirm dialog. Active bookings are cancelled, and personal data is anonymised [C-H1]. |

### 4.2 Barber

| ID | Story | Acceptance criteria |
|---|---|---|
| B1 | New booking push | Client name, phone (tappable), service, time, visit count and no-show count, "🆕 Yangi mijoz" (new client) when it applies, and the comment. Buttons: `✅ Qabul qilish`, `❌ Rad etish`, `🕒 Boshqa vaqt taklif qilish`. For an auto-confirmed booking it is an information message only. |
| B2 | Accept / reject | Accept → CONFIRMED and the client is notified. Reject → reason `Band` (busy) / `Kasal` (sick) / `Boshqa…` (other, free text) / `Sababsiz` (no reason) → REJECTED, the slot is freed and the client is notified. Every copy of the decision message (the barber's and the admins') is edited to the outcome. A repeated tap is harmless. A tap up to 10 min after expiry still confirms if the slot is free [C-B5]. |
| B3 | Propose a time | Pick from up to 8 free slots (same day, then the next 2 days). The slot is held for the client for 30 min. At most 2 proposals per booking [#21]. |
| B4 | Today / upcoming | `📅 Bugungi navbat` and `/today` [#25], and `🗓 Kelgusi kunlar` (upcoming days) → pick a day. Each booking has ✅ Bajarildi (done), 🚫 Kelmadi (no-show, offered from start + 15 min) and ❌ Bekor qilish (with a reason). |
| B5 | Done | COMPLETED, the bonus is applied or earned, and the client gets a thank-you with the new balance and a rebook button. |
| B6 | No-show | Allowed from start + 15 min. NO_SHOW, the counter goes up, and the client gets a polite note. An auto-completed booking can be flipped to no-show within 24 h, and its stamps are reversed [#30]. |
| B7 | Busy now | `⏸ Hozir bandman` → 30 or 60 min block starting now. Nothing is cancelled. The bot warns when the block overlaps a booking [C-F1]. |
| B8 | Block time | `⛔ Vaqtni band qilish`: a day, then a whole day or a from–to range, then a reason. If bookings conflict, choose "cancel and notify the clients" (each client gets a rebook button [#31]) or "keep them". Allowed only when `barbers_can_block_time` is on. |
| B9 | Manual walk-in | `➕ Qo'lda yozish`: service, day, slot (lead time ignored), name, phone (optional). The booking is CONFIRMED and has source=manual. It is linked to a bot client only when exactly one client has that **verified** phone [C-D3]. No client pushes are sent, except the visit check C19. |
| B10 | Morning summary | At 08:00: today's list. Every pending request comes as its own message with decision buttons [C-B1]. |
| B11 | Nightly digest | At 23:00: past CONFIRMED bookings that are not marked yet, with Done and No-show buttons. Bookings left unmarked are auto-completed 12 h after their end [#30]. |
| B12 | Pending flow | Re-ping after 15 min, sent to the barber **and the admins**, with buttons [#23][C-B3]. Expiry follows §5.8. |

### 4.3 Admin

| ID | Story | Acceptance criteria |
|---|---|---|
| A0 | First run | With no admin configured, the bot logs a one-time link `/start setup_<token>`. The first person to open it becomes admin [C-G1]. |
| A1 | Barbers | Create and edit: name, photo, bio, services, step, buffer, auto-accept, order. Invite link valid for 24 h. **Approve or deny** link requests. Unlink. Deactivating a barber previews their future bookings and cancels them, with client notifications [C-B6]. Barbers show an "unlinked" or "unreachable" badge. |
| A2 | Services | Name, duration (a multiple of 15, at most 240), price ≥ 0, active flag, order. Services are never deleted [#74]. |
| A3 | Working hours | Per barber and weekday, with a break. "Copy to all weekdays". Shrinking the hours lists the affected bookings without cancelling them. |
| A4 | Days off | Per barber or shop-wide (Navro'z, Hayit), with the conflict flow from B8. |
| A5 | Bookings | Filter by day, barber and status. Search by phone or name. 10 per page. Cancel with a reason [#76]. |
| A6 | Settings | Every key in ARCHITECTURE §4.4, grouped. The shop location is set by sharing a location. Values are validated, including the cross-field rules. |
| A7 | Clients | Phone search (at least 4 digits, at most 10 results, audited). Profile: history, no-shows, bonus, soft-block. Bonus ± with a required comment [#48]. Block and unblock: blocking cancels the client's active bookings; staff and yourself cannot be blocked [#69]. Reset no-shows. |
| A8 | Stats | Today, 7 days, this month, or a custom range. Per barber: total, completed, auto-completed, manual, no-shows, cancellations, rejected, expired, revenue. Also top services and new clients [#77]. |
| A9 | Admins | List of env and promoted admins. Promote by tg id or by a forwarded message. Demote promoted admins. |
| A10 | Alerts | An unreachable barber, a crash (rate-limited), a manual visit the client denied, a low rating [P2], and the nightly backup file. |

---

## 5. Business rules

### 5.1 Slots
- Candidates are `work_start + k × step`, **plus the end of every busy block** [C-F4].
- The occupied interval is `[s, s + duration + buffer)`. The service itself must end by closing time. The buffer may run past closing.
- A slot is free when it does not touch the break, time off (barber's or shop-wide) or any active booking's `[start, start + duration + buffer)`. It must also be at or after `ceil_to_15(now + min_lead)` and inside the horizon.
- Everything lies on the 15-minute grid.

### 5.2 Double booking
- The `booking_slots` 15-minute cell table has a primary key on (barber, cell). An overlapping insert fails inside the database.
- A losing client sees "Afsus, bu vaqt hozirgina band qilindi" (sorry, this time was just taken) and a refreshed list of slots.
- Availability is re-checked at confirm time.
- A failed attempt leaves no rows behind [C-A1].
- Stale buttons: every callback is re-validated, wizard callbacks must come from the current wizard message [C-E2], and a repeated identical tap within 1 s is ignored.

### 5.3 Client limits
- At most 3 active future bookings and 5 bot requests per local day, counted per account **and per phone** [C-D1][#66].
- A client's bookings may not overlap each other. Back-to-back bookings with the same barber are allowed, for example for families [C-E5].
- Throttle: 30 actions per minute per user, messages included [C-H3].

### 5.4 Today and time
- All local-day logic uses the shop timezone. Stats and lists bucket by local day [C-C6].
- `SHOP_TZ` (never `TZ`) must have a UTC offset that is a multiple of 15 minutes. `tzdata` is installed [C-C1][C-C2].

### 5.5 Schedule changes
- A day off or block over existing bookings shows a preview first. The barber or admin then chooses to cancel the bookings and notify the clients (each gets a rebook button) or to keep them.
- Deactivating a barber always cancels their future bookings, after a preview.
- Shrinking working hours lists the affected bookings without cancelling them.

### 5.6 Cancellation
- Free until `free_cancel_hours` (2) before start.
- After that, cancelling a CONFIRMED booking counts as a late cancel. Withdrawing a PENDING request is never late.
- Optionally, every N late cancels count as 1 no-show (default off).

### 5.7 No-shows and soft-block
- The barber can mark a no-show from start + 15 min.
- 3 no-shows within 90 days soft-block the client: their bookings always need approval and they see a warning. The admin can reset this.
- A hard block is admin-only.
- Bookings left unmarked are auto-completed `auto_complete_after_hours` (12) after their end. The barber can flip such a booking to no-show within 24 h.

### 5.8 Pending and expiry
- **The booking is PENDING when** the barber has auto-accept off, or the client is new (no completed visit yet, setting on by default), or the client is soft-blocked. Otherwise it is CONFIRMED immediately [C-B3].
- **Expiry time:** `cutoff = start − 10 min`.
  - If the barber is working when the request arrives, the decision window starts then. Otherwise it starts when the barber's next working window opens.
  - The booking expires `pending_timeout` (60 min) after the window starts, but no later than the cutoff.
  - If the next window only opens at or after the cutoff, the booking expires at the cutoff. The 08:00 morning message brings the request to the barber with buttons.
  - The decision window is never shorter than 15 min [C-B1][C-B2].
- **Re-ping:** 15 min after creation (during working hours), sent to the barber and the admins.
- **Unreachable or blocked barber:** the decision goes to the admins at once [#23][C-B4].
- **Late accept:** within 10 min after expiry, if the slot is still free [C-B5].
- A client who has a proposal to answer has 30 min, capped by the cutoff.

### 5.9 Phone
- **Bot bookings:** only the client's own Telegram contact (`contact.user_id == from.id`), stored as verified.
- **Manual input** (barber walk-ins only) is normalised to `+998XXXXXXXXX`, with the relaxed pattern `^\+998\d{9}$`.
- Foreign contact numbers are allowed (setting) and flagged.
- A typed phone never overwrites a verified one. Walk-ins are linked only to a verified phone that matches exactly one client [C-D3].

### 5.10 Names and text
- Names are 2–50 characters: letters in any script, spaces and `'` `ʻ` `ʼ` `` ` `` `-`.
- Every user-supplied value is HTML-escaped in every outgoing message.

### 5.11 Telegram specifics
- **Private chats only.** The bot leaves groups it is added to [#90][C-E7].
- **Barber blocked the bot:** they are marked unreachable and the admins are alerted. They become reachable again automatically when they next write to the bot [C-B4].
- **Client blocked the bot:** the failure is logged only.
- **Callback data:** at most 64 bytes.
- **FSM:** stored in the DB, so it survives restarts [C-E3]. If the state is still lost, the bot answers "Sessiya tugadi, qaytadan boshlang" (session ended, start again).
- **Exits:** the menu buttons, `/menu` and `/cancel` work from any state [C-E1].
- **Edit-in-place:** edits never crash on "not modified" or on a text/photo switch [C-E4]. A global error handler answers every callback.
- **Sending:** paced at 25 messages per second or less, `RetryAfter` is honoured, and nothing is sent while a database transaction is open [C-A2].
- **Single bot instance:** enforced with a lock file [C-A4].

### 5.12 Time
Datetimes are always timezone-aware. Daily jobs are driven by the scheduler tick with "done today" markers, so they survive restarts and setting changes [C-C4]. Missed reminders past their window are dropped, not burst-sent [C-C5].

### 5.13 Snapshots
Each booking snapshots the price, duration, service name, buffer and bonus cost [#75][C-A5].

### 5.14 Reminders after restarts
Reminders and other notifications live in the outbox table. A row is claimed before it is sent (at most once). Stale rows are cancelled per kind:

| Kind | Cancelled if not sent within |
|---|---|
| 24 h reminder | 3 h |
| 2 h reminder | 30 min |
| Re-ping | until expiry − 5 min |
| Return reminder | 3 days |
| Review | 24 h |

---

## 6. Loyalty

**Mode (setting, switchable only while every balance is 0):**
- **Stamps (default) [#43]:**
  - Each completed visit earns +1, at most 1 per client per local day [C-D2].
  - 5 stamps make the 6th visit free. The admin can instead set a percent discount.
  - A redeemed visit earns nothing.
- **Cashback [P2][#42]:**
  - A completed visit earns `cashback_percent` (5%) of the paid price, in so'm.
  - The client can redeem up to `cashback_max_redeem_percent` (50%) of the price.

**Redeeming [#44]:**
- The client switches it on in the booking summary. The cost is reserved on the booking (available = balance − reserved).
- It is debited only on COMPLETED.
- Cancel, reject, expiry and no-show release the reservation.
- If the balance became too small (for example after a settings change), the visit completes at full price and the barber is told [C-A5].

**Welcome bonus [#45]:** +1 stamp (or a so'm amount) on the client's first completed visit.

**Referral [P2][#46]:**
- The link is `ref_<opaque code>`.
- When the friend's first visit is completed (not auto-completed), the referrer gets the referral bonus.
- Self-referral and a referrer with the same phone are rejected [C-D6].

**Walk-ins:** a manual booking earns only after the client confirms the visit in the bot [C-D2].

**Ledger:**
- Append-only. Reasons: earn, redeem, welcome, referral, manual, reversal.
- Unique per (user, booking, reason), and one earn per user per day.
- Admin adjustments require a comment and are audited.
- The balance never goes below 0.

---

## 7. Notifications matrix

| Event | Client | Barber | Admin |
|---|---|---|---|
| Booking created, PENDING | "yuborildi" + withdraw | decision buttons | decision buttons if the barber is unreachable or the client needs approval |
| Booking created, auto-confirmed | confirmed + address/pin | info message | – |
| Confirmed (incl. late accept) | ✅ + address/pin | all decision messages edited | – |
| Proposal | accept/decline buttons | decision messages edited | – |
| Proposal answered | ack | ✅ or ❌ | – |
| Rejected | apology, reason, rebook | messages edited | – |
| Expired | apology + rebook | messages edited | ✅ |
| Client cancelled or withdrew | ack | ✅ | – |
| Staff cancel / day off / deactivation / client blocked | apology + rebook | ack | – |
| Re-ping (15 min) | – | ✅ buttons | ✅ buttons |
| Reminder 24 h | ✅ Kelaman / Bekor qilish | – | – |
| Reminder 2 h | ✅ (unless already acknowledged) | – | – |
| Morning summary 08:00 | – | ✅ list + pending decisions | pending requests of unreachable barbers |
| Nightly digest 23:00 | – | unresolved bookings with buttons | – |
| Completed | thanks + balance + rebook | – | – |
| Manual visit completed | "Were you here?" [Ha]/[Yo'q] | – | ✅ if the client says no |
| No-show | polite note | – | – |
| Return reminder | ✅ + rebook | – | – |
| Rating ≤ 3 [P2] | – | – | ✅ |
| Barber unreachable / crash / barber link request | – | – | ✅ |
| Nightly backup | – | – | ✅ file (env admins) |

Dates are shown as `26-oktabr, dushanba, 10:30`. 2026-10-25 is a Sunday (yakshanba) and 2026-10-26 a Monday (dushanba) [C-C7].

---

## 8. Menus

**Client** (reply keyboard):
```
[✂️ Navbatga yozilish]
[📋 Mening navbatlarim] [🎁 Bonuslarim]
[💈 Ustalar va narxlar] [📍 Manzil va aloqa]
```
**Barber:**
```
[📅 Bugungi navbat] [🗓 Kelgusi kunlar]
[⏸ Hozir bandman] [⛔ Vaqtni band qilish]
[➕ Qo'lda yozish] [👤 Mijoz menyusi]
```
**Admin:**
```
[💈 Sartaroshlar] [🧾 Xizmatlar]
[🕒 Ish vaqti] [🌴 Dam olish kunlari]
[📋 Navbatlar] [👥 Mijozlar]
[📊 Statistika] [⚙️ Sozlamalar]
[🛡 Adminlar] [🔄 Rol]
```
**Commands** (localised in the ☰ menu [#88]): `/start`, `/menu`, `/cancel`, `/my`, `/today` (barbers), `/whoami`, `/lang`, `/delete_me`, `/help`.

---

## 9. Non-functional requirements

- **Stack:**
  - Python code targets 3.10 and runs on 3.12. The production image is `python:3.12-slim`.
  - aiogram 3.31, SQLAlchemy 2.0.55 async, SQLite in WAL mode (Postgres-ready), Alembic, APScheduler 3.11 (one tick job), tzdata.
  - Long polling.
- **Deploy:** a systemd unit or Dockerfile with restart-always. Migrations run as a separate step and never on start [#94]. One instance only.
- **Data path:** absolute, resolved from the project root [C-G4].
- **Backups:** nightly online SQLite backup, 14 kept, integrity-checked, and sent to the env admins [#93][C-G5].
- **Demo data:** only with `--demo` [#92][C-G3].
- **Performance:**
  - A wizard step answers in under 500 ms.
  - Computing 7 days of slots for one barber takes under 50 ms, and for "any barber" with 5 barbers under 200 ms.
  - No database transaction stays open across a Telegram call [C-A2].
- **Security:**
  - Role checks happen in services on every read and write [C-D5].
  - Barbers see full phone numbers only for bookings from yesterday onward [C-H2].
  - Barber linking needs admin approval.
  - All user values are HTML-escaped.
  - No secrets are kept in the repo.
- **Privacy:**
  - Consent screen with a version number [#73].
  - `/delete_me`.
  - Hosting in Uzbekistan for ZRU-547 localisation (the owner chooses the provider) [C-H1].
  - Phone searches are audited.
- **Logging:** structured logs, admin alerts on errors, and the resolved timezone logged at start.
- **Tests:** the list in ARCHITECTURE §10.

---

## 10. Release scope

### v1.0 core (P1, must ship)
- Onboarding (language, consent), hub, and the service → barber/any → day → slot → comment → contact → confirm flow.
- Barber-first entry, rebook, withdraw, my bookings, cancel.
- Prices, barber cards, address, map buttons, generated hours.
- Barber decisions: accept, reject, propose; expiry; admin fallback; late accept.
- Today and upcoming lists, done/no-show, auto-complete with flip, busy-now, block time, manual walk-ins, morning summary, nightly digest.
- Reminders 24 h and 2 h, the return reminder, the outbox.
- Stamps loyalty: ledger, redeem, welcome bonus, manual adjustment, manual-visit confirmation.
- Admin: CRUD, approvals, admins, bookings, clients, settings, stats.
- Limits, throttling, private-only, single instance, backups, Alembic, setup token, `/delete_me`.
- Uzbek Latin.

### v1.0 stretch (P2, cut line at M4)
Russian locale, cashback mode, referral, month calendar, rating after the visit.

### v1.1, later, skip
The roadmap is in PLAN.md §9. Highlights for v1.1:
- Waitlist [#86] and reschedule [#16].
- Broadcast [#79] and CSV export [#80].
- Client notes [#81], reviews with text and photo, map review links [#56], rating on barber cards [#55].
- Birthday bonus [#40], service categories [#12], per-barber price UI [#14], barber 1 h ping [#32], barber stats [#33].
- Reception/admin group [#83][C-F2], Uzbek Cyrillic, "ask a human" [#63], fake Bot API tests [#91], Postgres hosting option.

---

## 11. Open questions for the owner

Q1–Q8 have defaults in ARCHITECTURE §0.4, and every default can be changed in Settings. These still need the owner:
- **Q9:** Hosting provider in Uzbekistan (UzCloud or a local VPS) [C-H1].
- **Q10:** Does the shop work as a live first-come queue ("navbat") as well as appointments? This affects a v1.1 "Hozir navbatda N kishi" (N people in the queue now) view [C-F3].
- **Q11:** Should new clients' first bookings need approval (the default is yes)?
- **Q12:** Welcome and referral bonus amounts.
