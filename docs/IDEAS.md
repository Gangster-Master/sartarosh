# Feature catalogue: ideas we can borrow for the Uzbek barbershop Telegram bot

> **Status:** final. Sections 0–11 are the R&D catalogue as delivered (verbatim). Section 12 records the Judge's decision for every item. Section 12 is authoritative where it differs from the "Rec." column. Where an item is adopted, ARCHITECTURE.md and PLAN.md give the exact contract.

On 2026-10-24 I read the READMEs of every candidate. For Xaveron I also read the code: `rules.py`, `slots.py`, `stats.py` and `scheduler/jobs.py`. I ran 9 more GitHub searches and found 10 more salon or barber bots. I also read the feature pages of Fresha, Booksy and Altegio. Altegio is the international brand of YCLIENTS.

**Cost:** S is under 1 day, M is 1–3 days, L is more than 3 days, all for one developer.
**Recommendation:** MVP, v1.1, later, or skip.

## 0. License map (what we may take)

| Source | License | What we may take |
|---|---|---|
| [Xaveron/barbershop-bot](https://github.com/Xaveron/barbershop-bot) | MIT | Ideas and code. If we copy code, keep the MIT notice and name the author in `THIRD_PARTY_NOTICES`. |
| [DigitalJacob/booking_bot](https://github.com/DigitalJacob/booking_bot) | MIT | Ideas and code, with the same notice. |
| [noXplode/aiogram_calendar](https://github.com/noXplode/aiogram_calendar) (191★) | MIT | We can vendor the inline calendar keyboard with the notice. AinurMingazov did exactly this. |
| [Tishka17/aiogram_dialog](https://github.com/Tishka17/aiogram_dialog) (898★) | Apache-2.0 | We can use it as a library. I still prefer hand-made keyboards. |
| [6HosT-io/VovaBarber](https://github.com/6HosT-io/VovaBarber), [AinurMingazov/aiogram_barber_bot](https://github.com/AinurMingazov/aiogram_barber_bot), [bladsvytro/nail-booking-bot](https://github.com/bladsvytro/nail-booking-bot), [nfbl/salon-ai-admin-bot](https://github.com/nfbl/salon-ai-admin-bot) | Apache-2.0 / MIT | Code is allowed, but nothing in them is worth copying. Ideas only. |
| [manch1915/tg-booking-bot](https://github.com/manch1915/tg-booking-bot), [Rissoedych/beauty-booking-bot](https://github.com/Rissoedych/beauty-booking-bot), [frolovelo/saloon_bot](https://github.com/frolovelo/saloon_bot), [mittixx/lune-nails-booking-bot](https://github.com/mittixx/lune-nails-booking-bot), [T4rk123/ManicureSalon-TelegramBot](https://github.com/T4rk123/ManicureSalon-TelegramBot), [ilnrzakirov/barber-bot](https://github.com/ilnrzakirov/barber-bot) | No license | **Ideas only. Copy no code.** |
| [nazgool97/salon_bot](https://github.com/nazgool97/salon_bot) | Open-core, NOASSERTION | Ideas only. |
| [andryhasayan-source/beauty-salon-bot](https://github.com/andryhasayan-source/beauty-salon-bot) | Closed commercial (README only) | Ideas only. |
| Fresha, Booksy, Altegio/YCLIENTS | Commercial products | Feature ideas only. |

New find: **manch1915/tg-booking-bot** ("Brass & Blade", created 2026-10-19, no license). It is the only bot I found that has **bonuses, cashback, a referral link, a waitlist, reviews and deposits**. Read it for ideas, but copy no code. Its Mini App and Vue admin break our "Telegram only" rule.

## 1. Booking flow and UX

| # | Feature | What it is | Seen at | Value for an Uzbek barbershop | Cost | Rec. |
|---|---|---|---|---|---|---|
| 1 | Flow: service → barber (or **"Any free barber"**) → day → time → summary | Linear inline flow. "Any free barber" merges the free slots of all barbers and picks one when the client confirms. | Rissoedych, manch1915, andryhasayan, nazgool97 | High. Walk-in-style clients don't care who cuts them. | M | **MVP** |
| 2 | Barber-first entry | Start from a barber, then pick a service. | nfbl, bladsvytro | Medium. Regulars ask for "Akmal aka". | S | **MVP** (button on barber card) |
| 3 | Month calendar showing only bookable days | Inline month grid. Full or closed days are shown greyed or as "·" and only answer the tap with a short notice. ←/→ change month within the booking horizon. | DigitalJacob, AinurMingazov (vendored noXplode, MIT), T4rk123 | High. | S (vendor noXplode) / M (own) | **MVP** |
| 4 | Quick day buttons: Bugun / Ertaga / Indinga / Boshqa kun | Most bookings are for the next 1–3 days, so this skips the calendar. A blocked day gets the label "(band)" and an alert. | VovaBarber | High. Fewest taps. | S | **MVP** (with calendar behind "Boshqa kun") |
| 5 | Slot grid grouped as Ertalab / Kunduzi / Kechqurun | 3–4 columns of `HH:MM` buttons with group headers. | manch1915 (Mini App time board) | High readability on phones. | S | **MVP** |
| 6 | Only real free slots shown | Pure `generate_slots()` that takes weekly windows, overrides, busy intervals, service length, step and `not_before = now + min_lead`. | Xaveron `app/services/slots.py` (MIT, copyable), DigitalJacob | Core. | M | **MVP** |
| 7 | Edit-in-place "single screen" / sticky hub | Each step edits the same message (`edit_text`) so the chat doesn't fill up. `/start` refreshes the hub and `/menu` posts a new one. | DigitalJacob, Rissoedych | High. A clean chat. | M | **MVP** |
| 8 | Real "Back" with preserved context | A navigation stack in FSM data, so Back goes to the previous step with earlier choices kept. | nazgool97, DigitalJacob | High. | S | **MVP** |
| 9 | Inline "Bekor qilish" on every multi-step flow | Abort with a button instead of a slash command. | DigitalJacob | Medium. | S | **MVP** |
| 10 | Stale-button / double-tap guard | Re-check status on the server for each callback and answer "Bu tugma eskirgan". Debounce double taps. | DigitalJacob, mittixx | Prevents duplicate bookings. | S | **MVP** |
| 11 | Optional comment step | "Izoh qoldirish" or "Izohsiz yuborish". | VovaBarber | Medium (e.g. "soqol ham"). | S | **MVP** |
| 12 | Category → service | Groups such as Soch / Soqol / Kompleks / Bolalar. | Rissoedych, manch1915, andryhasayan | Medium. Only needed with more than 8 services. | S | v1.1 |
| 13 | Multi-service booking (cut + beard) | Durations add up. Composite services. | nazgool97 | High. "Soch + soqol" is the usual order. | M | **MVP** as preset combo services. Free combination in v1.1. |
| 14 | Per-barber duration and price | A senior barber is slower or more expensive. | nazgool97 (per-master durations), manch1915 (own prices per service). Xaveron explicitly lacks it. | Medium. | M | v1.1 (nullable override columns in `barber_services` from day one) |
| 15 | **Rebook "same as last time"** | One button on the hub: "Avvalgidek yozilish (Akmal, Soch+soqol)". Jumps straight to the day picker. | Fresha (rebook favourites) | High. Barbershop clients repeat the same order. | S | **MVP** |
| 16 | Reschedule | Move a booking to another slot. The barber approves again. Keep the old slot until the new one is confirmed. | VovaBarber, mittixx, manch1915, Xaveron (`ensure_can_reschedule`) | High. | M | v1.1 (MVP clients cancel and rebook) |
| 17 | Withdraw request before the barber answers | "So'rovni qaytarib olish" under "So'rov yuborildi". | VovaBarber | Medium. | S | **MVP** |
| 18 | `.ics` calendar file after booking | Send it as a document. | manch1915 | Low in UZ. | S | later |
| 19 | Recurring appointments | "Every 2 weeks at Saturday 10:00." | Altegio | Medium for regulars. | M | later |

## 2. Barber side

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 20 | Accept / Decline on the new-booking push | One tap to accept. Decline asks for confirmation and an optional reason. A stale button is rejected. | DigitalJacob, VovaBarber, andryhasayan | Core requirement. | S | **MVP** |
| 21 | **"Suggest another time"** | Instead of only declining, the barber proposes a time. The client gets **Qabul qilish / To'g'ri kelmaydi**, then the barber can suggest again or close the request. | VovaBarber | High. Saves the client and avoids lost bookings. | M | **MVP** (with structured slot buttons, not free text) |
| 22 | Pending expiry | The pending booking holds the slot for N minutes, then is auto-rejected and the client is told. | R&D report; nazgool97 ("hold cleanup" worker) | Core. | S | **MVP** |
| 23 | Fallback to admin when the barber is unlinked or silent | No barber `chat_id`, or no answer in X minutes, means the admin or the admin group gets the request. | andryhasayan, VovaBarber (admin group) | High. | S | **MVP** |
| 24 | Barber linking by one-time deep link | `t.me/bot?start=inv_<token>`. | manch1915, R&D report | Core. | S | **MVP** |
| 25 | `/today` (Bugun) agenda | The barber's list for the day with client name and phone (tap to call), status and service. | manch1915, DigitalJacob (week → day → card) | High. | S | **MVP** |
| 26 | Walk-in / phone booking by the barber | Search a client by phone or name, or create one with name and phone. The booking is confirmed at once. A walk-in gets no client pushes. Duplicate phones are reused. | DigitalJacob (`manual_clients`), manch1915 | High. Most UZ bookings still come by phone. | M | **MVP** |
| 27 | Time off and breaks with an overlap warning | A full day, a date range, or hours within a day. "Overlaps 2 bookings, save anyway?" | DigitalJacob | High (lunch, Friday prayer, personal days). | M | **MVP** |
| 28 | Gap between appointments / buffer | `gap_minutes` after each visit. | DigitalJacob, Altegio | Medium (cleanup time). | S | **MVP** (setting) |
| 29 | Minimum lead time and slot step settings | Lead in minutes. Step defaults to service length. | DigitalJacob, Xaveron, VovaBarber (~15 min lead) | Core. | S | **MVP** |
| 30 | Done / No-show marking | Buttons on the card after the visit time. Unmarked bookings auto-close to done after X hours. | manch1915 ("came / no-show"), Xaveron (`complete_past_appointments` auto job), andryhasayan | Core for loyalty. | S | **MVP** (manual buttons plus an auto-done fallback that the barber can flip to no-show within 24h) |
| 31 | Closing a day notifies the affected clients | The barber or admin closes a day, and every booked client gets a message with a "rebook" button. | T4rk123 | High. | S | **MVP** |
| 32 | Barber reminders | The barber gets the evening-before agenda and a ping 1h before, with client name and phone. | DigitalJacob | Medium. | S | v1.1 (MVP: morning agenda only) |
| 33 | Personal stats for the barber | Visits and revenue this month. | andryhasayan, mittixx | Medium (motivation). | S | v1.1 |

## 3. Reminders, retention and notifications

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 34 | Persistent `notifications` table (outbox) | `UNIQUE(booking_id, kind)`. The scheduler scans due rows. Use `FOR UPDATE SKIP LOCKED`, retry network errors, give up on bots the user blocked. | Xaveron, manch1915, Rissoedych | Core. Survives restarts. | M | **MVP** |
| 35 | 24h and 2h reminders with Confirm / Cancel buttons | The client can confirm or cancel from the reminder. If confirmed at 24h, skip the morning one. | VovaBarber, Xaveron, DigitalJacob, mittixx | High. Fewer no-shows. | S | **MVP** |
| 36 | Configurable reminder offsets | Preset choices: 2 / 6 / 12 / 24 / 48 hours. | andryhasayan | Low. | S | later |
| 37 | **"Time for a haircut" return reminder** | N weeks after the last done visit, if nothing is booked, send a push with a rebook button. | Xaveron (`RETURN_REMINDER_WEEKS`), Altegio ("win-back") | **Very high** for a barbershop with a 3–4 week cycle. | S | **MVP** |
| 38 | Status-change notifications to both sides | Accepted, declined (with reason), cancelled (with reason), rescheduled. | DigitalJacob, andryhasayan | Core. | S | **MVP** |
| 39 | Delivery-failure alert to admin | "Could not deliver N reminders." | Xaveron `jobs.py` | Low, cheap. | S | v1.1 |
| 40 | Birthday greeting and bonus | Ask for the birth date (optional). Send a greeting plus bonus points or a discount. | Altegio | Medium–high (common in UZ marketing). | S | v1.1 |

## 4. Loyalty, bonuses, referral and money (our main difference from the other bots)

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 41 | Append-only loyalty ledger | `loyalty_ledger(user_id, delta, reason, booking_id, created_at)`. Balance is the sum of `delta`. | R&D report; manch1915 (bonus balance) | Core requirement. | M | **MVP** |
| 42 | Points per completed visit (cashback %) | Credit only when the visit is marked done. No-shows earn nothing. | manch1915 (cashback after every visit), Altegio (bonus/cashback) | Core. | S | **MVP** |
| 43 | Visit-count stamp card | "Every 6th haircut is free." Progress shown as ✂️✂️✂️▫️▫️▫️. | Altegio (loyalty cards), Fresha (rewards for repeat visits) | **Very high.** Easy to understand and familiar from paper cards. | S | **MVP** (either this or points; the admin chooses the mode) |
| 44 | Redeem at booking | On the summary screen: "Use 15 000 so'm of bonus?" Points are reserved and only debited when the booking is confirmed or done. They are returned on cancel or reject. | Altegio ("applied automatically at booking"), R&D edge cases | Core. | M | **MVP** |
| 45 | Welcome bonus | Points for the first booking or for completing the profile. | manch1915 | Medium (acquisition). | S | **MVP** |
| 46 | **Referral deep link** | `t.me/bot?start=ref_<code>`. When the friend's **first visit is done**, both sides get a bonus, so self-referral or never-visiting accounts get nothing. Share it with `switch_inline_query` or a share URL. | manch1915, Altegio (referral program) | **High.** In UZ, word of mouth is the main growth channel. | M | **MVP** (if time allows), else v1.1 |
| 47 | Loyalty tiers | Bronze / Silver / Gold with rising %. | Fresha | Low for one shop. | M | later |
| 48 | Manual bonus adjustment by admin | Correct or gift points with a reason, written to the ledger. | manch1915 | Medium (disputes). | S | **MVP** |
| 49 | Bonus expiry and expiry reminder | Points expire after N months, with a warning push. | Altegio (discount-expiry messages) | Low. | M | later |
| 50 | Deposits / prepayment | Pay part of the price when booking. A late cancel keeps the deposit. | manch1915 (Stars, Stripe), nazgool97, Booksy, Fresha, Altegio | Medium. Cuts no-shows, but UZ clients expect to pay cash. | L | later. If done, use Click or Payme through Telegram Payments (check that BotFather offers these providers). |
| 51 | Tips in Telegram Stars after a good review | | manch1915 | Low in UZ. | M | skip |
| 52 | Gift certificates / memberships (abonement) | Prepaid visits debited per use. | Altegio, YCLIENTS | Medium (gift for Hayit or a birthday). | L | later |
| 53 | Off-peak "happy hour" discounts | Lower price at quiet times, such as weekday mornings. | Booksy, Fresha | Medium. Fills empty slots. | M | later |

## 5. Trust, reviews and content

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 54 | **Review request after the visit** | X hours after done, ask for a rating of ⭐1–5 with an optional text or photo. The request is stored in the DB so it survives restarts and is sent only once. | Rissoedych (delayed, persistent), manch1915, Altegio, Fresha (verified reviews only from real bookings), Booksy | High. Low ratings go privately to the admin. | M | v1.1 (MVP: rating only) |
| 55 | Barber rating on the barber card | Average rating and number of reviews. | manch1915, Booksy | Medium. | S | v1.1 |
| 56 | Ask 4–5★ clients to review on Google or Yandex Maps | Deep link to the shop's map review page. | Fresha (auto-posting to Google) | High for discovery (2GIS, Yandex and Google are used in Tashkent). | S | v1.1 |
| 57 | Barber card with photo, bio and portfolio | `send_photo` plus a "Ishlari" button that sends an album with `send_media_group`. | nazgool97 (master card), Rissoedych, bladsvytro (`answer_media_group`), Booksy (portfolio), T4rk123 (Pinterest link) | High. People pick a barber from photos of their work. | S | **MVP** (photo and bio), v1.1 (portfolio album managed by the barber) |
| 58 | Price list screen | Services with price and duration, prices formatted like "50 000 so'm". | all | Core. | S | **MVP** |
| 59 | Location, map and contacts | `send_venue` (pin plus address) and buttons for Yandex, Google and 2GIS. | Xaveron (`SHOP_MAPS_URL`), ilnrzakirov, Rissoedych ("О студии") | High. | S | **MVP** |
| 60 | Working-hours display | Generated from the `working_hours` table so it is never out of date, with today's hours shown as "Bugun: 09:00–21:00 (ochiq)". | Rissoedych, andryhasayan | High. | S | **MVP** |
| 61 | Editable welcome and "About us" text | The admin edits them in the bot, with HTML formatting. | andryhasayan | Medium. No redeploy needed. | S | v1.1 |
| 62 | FAQ and AI answers | An LLM answers from the shop's own knowledge base. | nfbl | Low. Adds cost and risk. | L | skip, or a static FAQ later |
| 63 | "Ask a human" | Forwards the question to the admin chat, and the admin's reply is relayed back. | nfbl | Medium. | M | v1.1 |
| 64 | Channel subscription gate | Must subscribe to the shop channel before booking. | T4rk123 | Negative UX. | S | **skip** |

## 6. Anti-abuse and rules

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 65 | Own-contact verification | `request_contact`, then accept only if `contact.user_id == from_user.id`. | Rissoedych, R&D report | Core. | S | **MVP** |
| 66 | Active-booking limits | Maximum K active bookings and M requests per day for each client. | Rissoedych (3 active, 5 per day), Xaveron (`MAX_ACTIVE_APPOINTMENTS`), AinurMingazov (one active) | High. Stops one user blocking a whole day. | S | **MVP** |
| 67 | Cancel and reschedule deadline | Not later than N hours before the visit (`ensure_can_cancel`). Late cancels are blocked or flagged. | Xaveron `rules.py`, manch1915 (3h) | Core. | S | **MVP** |
| 68 | No-show counter, then require confirmation | After 2 no-shows the client's bookings always need barber approval or a deposit. | Booksy, Fresha (no-show protection, adapted) | High. | S | v1.1 |
| 69 | Ban and shadowban | Banned users get no reply at all. Guard rails: an admin cannot ban themselves or other staff. | DigitalJacob, andryhasayan | Medium. | S | v1.1 |
| 70 | Arithmetic captcha on the first visit | | Rissoedych | Low. The contact check is enough. | S | skip |
| 71 | Flood throttling | Token bucket per user. | Xaveron | Medium. | S | **MVP** (simple middleware) |
| 72 | Role reset when a staff phone moves to another account | | Rissoedych | Low for one shop. | S | later |
| 73 | Personal-data consent screen with a version | Asked again when the consent text version changes. | DigitalJacob, mittixx | Medium. UZ has a personal data law (ZRU-547); keep a one-screen notice. | S | **MVP** (short) |

## 7. Admin, analytics, marketing and export

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 74 | Admin CRUD in the bot | Barbers, services, prices, hours and day exceptions such as Navro'z and Hayit. Barbers and services with active bookings can be hidden but not deleted. | Xaveron, Rissoedych, andryhasayan | Core. | M | **MVP** |
| 75 | Price snapshot on the booking | Store the price at booking time so later price changes don't rewrite history. | Xaveron | Core for revenue stats and loyalty %. | S | **MVP** |
| 76 | All-bookings list with filters and pagination | Filter by status, date or barber. Search by name or phone (`/bookings Ali`). | andryhasayan, VovaBarber | High. | M | **MVP** (basic) |
| 77 | Stats screen | Today, month, revenue, cancellations, top services, top barbers. Xaveron does this in 4 SQL queries. | Xaveron `stats.py`, mittixx (expected revenue) | High for the owner. | S | **MVP** |
| 78 | Advanced analytics | No-show rate, occupancy, average ticket, retention, LTV, booking sources (deep-link `start` parameter), lost clients. | nazgool97, manch1915, Altegio | Medium. | M | v1.1 (no-show rate and source), later (LTV, retention) |
| 79 | **Broadcast to clients** | To all clients or a segment: lapsed (no visit in N weeks), new, regulars of one barber. Preview before sending, rate-limited at about 25 messages per second, with a delivered/blocked report. | manch1915 (segments), Rissoedych (send to everyone), Booksy, Altegio | High (promos, holiday hours). | M | v1.1 (all clients in MVP if time allows) |
| 80 | CSV/Excel export | Bookings, clients and the ledger. Neutralise formula injection: escape cells that start with `= + - @`. | Xaveron (`export.py`), nazgool97, manch1915, Altegio | Medium (accountant). | S | v1.1 |
| 81 | Client card with notes | Visit history, no-shows, spend, bonus balance and a free-text note such as "likes fade 2". | manch1915, Booksy (client cards), Altegio | High for barbers. | M | v1.1 |
| 82 | Google Sheets or Calendar mirror | Bookings mirrored to a Sheet or Calendar, with two-way sync. | frolovelo (Sheets as DB), VovaBarber (Calendar sync every 3 minutes) | Low–medium. Some owners like Sheets. | M–L | later (one-way export only) |
| 83 | Admin group chat for requests | New requests are posted in a staff group, and `/pending` works even after the message is deleted. | VovaBarber | Medium (shared visibility). | S | v1.1 |
| 84 | Payroll and commission per barber | | Altegio | Low for an MVP. | L | later |
| 85 | Inventory of consumables | | Altegio, nazgool97 roadmap | Low. | L | skip |

## 8. Waitlist

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 86 | **Waitlist when a day is full** | On a full day, show "Bo'shasa xabar bering" (notify me if a slot opens). The client chooses a day, an optional barber and a time window. When a cancellation or rejection frees a slot, notify the waiting clients, first come first served, with a "Book now" button. The slot is not held, so whoever taps first gets it. | manch1915, Booksy (automated waitlist within a chosen time frame), Fresha, Altegio | High. Weekend evenings fill up. | M | v1.1 |

## 9. Platform and i18n

| # | Feature | What it is | Seen at | Value | Cost | Rec. |
|---|---|---|---|---|---|---|
| 87 | Multi-language with a resolution chain | Explicit choice, then Telegram `language_code`, then the default. Stored per user. A test checks that every locale has the same keys. | Xaveron (`test_i18n.py`, ru/ro/en), DigitalJacob, VovaBarber | Core. | S | **MVP:** Uzbek Latin (default) plus Russian. v1.1: Uzbek Cyrillic, as a second locale file or by transliteration. |
| 88 | Telegram ☰ command menu localised | `set_my_commands` for each language. | DigitalJacob | Medium. | S | **MVP** |
| 89 | Business-logic error keys, not text | `RuleViolationError("rule.cancel_deadline", minutes=…)`, translated in the handler. | Xaveron `rules.py` | Keeps services free of i18n. | S | **MVP** |
| 90 | Private-chat-only filter | Ignore updates from groups and channels before they reach the DB. | Xaveron | Security. | S | **MVP** |
| 91 | Fake Bot API and demo client for end-to-end tests | Run the bot without real Telegram. | Xaveron (`tools/fake_bot_api.py`), manch1915 (feeds fake updates to the real dispatcher) | Medium. | M | v1.1 |
| 92 | Seed and demo data | `SEED_ON_START=true`. | Xaveron, bladsvytro, manch1915 | Useful for sales demos. | S | **MVP** |
| 93 | Backup script | `pg_dump`, gzip, keep 14 copies, print the restore command. | Xaveron `tools/backup.sh` (MIT) | High. | S | **MVP** |
| 94 | Don't run migrations automatically in the entrypoint in production | Xaveron's README documents a real incident caused by `alembic upgrade head` running on every container start. | Xaveron | Avoids data loss. | S | **MVP** (separate `migrate` service, as manch1915 does) |
| 95 | Telegram Mini App UI | | nazgool97, manch1915 | Breaks the "Telegram bot only" rule. | L | **skip** |
| 96 | Multi-tenant SaaS, tariffs, branches | | Xaveron | Not needed now. | L | skip. Keep a `shop_id` column only if we plan to resell. |

## 10. Suggested MVP cut (summary)

1. **Client:** language choice; own-contact check and short consent; hub that edits one message in place; the booking flow from #1, #3, #4, #5, #13 and #11; rebook "same as last time"; my bookings with cancel (deadline applies) and withdraw; prices; barber cards with photo; venue and map; working hours.
2. **Barber:** link by deep link; Accept, Decline or Suggest-time on the push; pending expiry with fallback to admin; today's agenda; walk-in booking; time off and breaks; done/no-show marking with auto-done.
3. **Retention:** outbox `notifications`; 24h and 2h reminders with confirm; "time for a haircut" after N weeks.
4. **Loyalty:** ledger; points or stamp card for done visits; redeem at booking (reserved, then debited when the booking is confirmed or done); welcome bonus; manual admin adjustment; referral deep link if time allows.
5. **Admin:** CRUD; day exceptions; bookings list; stats; price snapshot.
6. **Safety:** PG `EXCLUDE` constraint plus an advisory lock; booking limits; flood throttling; private chats only; backups.

**v1.1:** reviews and ratings with a Google/Yandex map link, waitlist, reschedule, broadcast to segments, CSV export, client notes, birthday bonus, no-show policy, admin group, Cyrillic locale.

## 11. Code worth copying (with attribution in `THIRD_PARTY_NOTICES`)

- `Xaveron/barbershop-bot` (MIT):
  - `app/services/slots.py`: `Interval`, `WorkWindow`, `DayOverride`, `resolve_work_windows` and `generate_slots`. Pure functions, about 150 lines.
  - `app/services/rules.py`: the `ensure_can_cancel` and `ensure_can_reschedule` pattern.
  - The return-reminder job in `app/scheduler/jobs.py`.
  - `tools/backup.sh`.
- `noXplode/aiogram_calendar` (MIT): the inline month calendar. Change it to grey out days that can't be booked, as AinurMingazov did.
- `DigitalJacob/booking_bot` (MIT): the `DataBaseMiddleware` that wraps each update in one transaction, and the `RepositoriesPort` Protocol pattern. Copy the patterns rather than the files, because it uses psycopg and raw SQL, not SQLAlchemy.

## Sources

**GitHub repos:**
- https://github.com/Xaveron/barbershop-bot
- https://github.com/DigitalJacob/booking_bot
- https://github.com/nazgool97/salon_bot
- https://github.com/frolovelo/saloon_bot
- https://github.com/6HosT-io/VovaBarber
- https://github.com/AinurMingazov/aiogram_barber_bot
- https://github.com/ilnrzakirov/barber-bot
- https://github.com/manch1915/tg-booking-bot
- https://github.com/Rissoedych/beauty-booking-bot
- https://github.com/bladsvytro/nail-booking-bot
- https://github.com/andryhasayan-source/beauty-salon-bot
- https://github.com/mittixx/lune-nails-booking-bot
- https://github.com/T4rk123/ManicureSalon-TelegramBot
- https://github.com/nfbl/salon-ai-admin-bot
- https://github.com/noXplode/aiogram_calendar
- https://github.com/Tishka17/aiogram_dialog

**Commercial products:**
- [Fresha features](https://www.fresha.com/for-business/features)
- [Booksy features](https://biz.booksy.com/en-us/features)
- [Altegio: all features](https://alteg.io/en/possibilities/)
- [Altegio: loyalty architecture](https://alteg.io/en/blog/loyalty-architecture/)
- [Altegio: client account in the booking widget](https://alteg.io/en/support/knowledge-base/4903552434461-personal-account-in-online-booking-widget/)
- [Altegio: chat-bot integration (Flowsell)](https://alteg.io/support/en/knowledge-base/7347127430429-2)
- [YCLIENTS memberships (Inc. Russia)](https://incrussia.ru/news/yclients-abonementi/)

The downloaded READMEs are in `/tmp/claude-1000/-home-master-start-up-sartarosh/31ec2771-0fdd-435b-ac84-263f837b2c1d/scratchpad/gh/`.

---

## 12. Judge decisions (authoritative)

**Tiers used below:**
- **MVP**: ships in v1.0. P1 means it is required for release.
- **MVP-P2**: in the v1.0 plan, but sits behind a cut line. At gate M4 the PM may move a P2 item to v1.1 without asking the owner, as long as its schema hooks stay in place. Every P2 item has its DB columns and service signatures in ARCHITECTURE.md from day 1, so a cut costs only UI work later.
- **v1.1**: the next release, planned in PLAN.md §9.
- **later** / **skip**: roadmap only (PLAN.md §9), or rejected.

**Code reuse decision:** we write our own code and use the MIT projects as references. Any file that is copied or closely adapted (most likely Xaveron `tools/backup.sh` or the noXplode calendar) must be listed in `THIRD_PARTY_NOTICES` with its MIT text, in the same PR. We copy nothing from unlicensed repos (manch1915, Rissoedych, frolovelo, mittixx, T4rk123, ilnrzakirov).

**Platform decision that changes item 6 of §10:** the MVP runs on SQLite (one shop, one process). The double-booking guard is the portable `booking_slots` cell table from ARCHITECTURE §3 instead of the Postgres `EXCLUDE` constraint. It gives the same guarantee on SQLite and Postgres. Postgres plus `EXCLUDE` is the v1.1 hosting option.

| # | Feature | Decision | How / why |
|---|---|---|---|
| 1 | Service → barber, "Farqi yo'q" (any free barber) | **MVP** | Main flow is service → barber/any → day → slot → comment → summary. `slots.get_free_slots_any()` merges the barbers' slots. `create_booking(barber_id=None)` picks the barber with the fewest active bookings that day, then `sort_order`, and moves on to the next candidate on `SlotTakenError`. This overrides the earlier "v1.1" ruling in PM §3.7. |
| 2 | Barber-first entry | **MVP** | A "✂️ Shu ustaga yozilish" button on the barber card starts the same flow with `barber_id` preset. The service list is filtered to that barber. |
| 3 | Month calendar | **MVP-P2** | Own implementation (about 60 lines, no vendored code). Opened from "📅 Boshqa kun". Days outside the horizon or without slots are shown as "·" and send `noop`. |
| 4 | Quick day buttons | **MVP** | Bugun / Ertaga / Indinga plus the next days within the horizon (at most 7 buttons), and "📅 Boshqa kun" when the horizon is over 7 days. |
| 5 | Grouped slot grid | **MVP** | Ertalab (before 12:00), Kunduzi (12:00–17:00), Kechqurun (17:00 and later), 4 per row. This also settles critic E6. |
| 6 | Real free slots only | **MVP** | Pure `compute_free_slots` (ARCH §4.10), our own code. Candidates also start at the end of each busy block (critic F4). |
| 7 | Edit-in-place hub | **MVP** | `bot/screen.py` `render()` helper (ARCH §6.7). It handles text↔photo switching and ignores "message is not modified". |
| 8 | Back with preserved context | **MVP** | `nav` stack in FSM data (ARCH §8). |
| 9 | Inline cancel on every flow | **MVP** | `WizCb(a="cancel")` on every step. Text prompts get an inline "❌ Bekor qilish". |
| 10 | Stale-button / double-tap guard | **MVP** | Server re-validation, a wizard `msg_id` check, and the `DebounceMiddleware` (1.0 s per user and callback data). |
| 11 | Optional comment | **MVP** | `BookingFlow.comment` step with "Izohsiz davom etish" (continue without a comment). Up to 300 characters, escaped. |
| 12 | Categories | v1.1 | `services.category` nullable column added in v1.1. |
| 13 | Combo services | **MVP** | As preset services (the seed has "Soch + soqol", 60 min). Free combination is v1.1. |
| 14 | Per-barber price/duration | v1.1 (UI) | The override columns in `barber_services` and the resolution logic ship in MVP. Only the admin UI waits. |
| 15 | Rebook "same as last time" | **MVP** | A hub button appears when the client has a COMPLETED booking. `bookings.get_last_completed()` returns the barber and service, and the flow jumps to the day step. |
| 16 | Reschedule | v1.1 | MVP: cancel and rebook. |
| 17 | Withdraw a pending request | **MVP** | `cancel_by_client` on PENDING. It is never a late cancel (critic D6). |
| 18 | `.ics` file | later | |
| 19 | Recurring bookings | later | |
| 20 | Accept / Decline on the push | **MVP** | |
| 21 | Suggest another time | **MVP** | Simplified. The barber picks one of up to 8 free slots (same day, then the next 2 days). The cells move to the proposed time at once, so the slot is held. The client gets ✅ Qabul qilish / ❌ To'g'ri kelmaydi. Accept → CONFIRMED. Decline → CANCELLED_BY_CLIENT with reason `proposal_declined`, never late. At most 2 proposals per booking. The client has 30 minutes to reply (capped by the cutoff). |
| 22 | Pending expiry | **MVP** | `compute_expire_at` was rewritten after critic B1/B2 (ARCH §4.11). |
| 23 | Fallback to admin | **MVP** | Admins get the decision message with working buttons in three cases: at creation if the barber is unreachable, at the re-ping, and in the morning summary for pending bookings. |
| 24 | Deep-link barber linking | **MVP** | `/start inv_<token>`, valid 24 h, and the admin must approve (critic D4). |
| 25 | Today agenda | **MVP** | `/today` and "📅 Bugungi navbat". The phone is shown as plain text, which Telegram makes tappable (critic E8). |
| 26 | Walk-in by barber | **MVP** | Phone search links the booking only to a user with a verified phone and exactly one match (critic D3). Name search is v1.1. |
| 27 | Time off and breaks with an overlap warning | **MVP** | Includes the one-tap "⏸ Hozir bandman 30/60" (critic F1). |
| 28 | Buffer between visits | **MVP** | `barbers.buffer_min ∈ {0,15,30}`. Cells cover `[start, end + buffer)`. |
| 29 | Lead time and step | **MVP** | |
| 30 | Done / no-show with auto-done | **MVP** | Manual buttons, plus the nightly digest at 23:00. Bookings still unresolved at `end + auto_complete_after_hours` (default 12) become COMPLETED with `auto_completed=1`. The barber can flip an auto-completed booking to NO_SHOW within 24 h, and the stamp is reversed through the ledger. |
| 31 | Closing a day notifies clients | **MVP** | Part of the time-off conflict flow. |
| 32 | Barber reminders | v1.1 | MVP sends a morning agenda that includes pending bookings with buttons. |
| 33 | Barber personal stats | v1.1 | |
| 34 | Notifications outbox | **MVP** | Claim → commit → send → mark (critic A2/A4). At most once. |
| 35 | 24h and 2h reminders | **MVP** | Kinds `r24` and `r2`. `r2` is skipped if the client already pressed "Kelaman" on `r24`. This replaces the earlier 1h client reminder. |
| 36 | Configurable offsets | later | |
| 37 | Return reminder | **MVP** | Kind `ret`, scheduled on COMPLETED at `start + return_reminder_weeks` (default 4, 0 = off). It is skipped if the client has a newer COMPLETED or any active booking. |
| 38 | Status-change notifications | **MVP** | |
| 39 | Delivery-failure alert | v1.1 | |
| 40 | Birthday bonus | v1.1 | |
| 41 | Ledger | **MVP** | Table `bonus_transactions`. |
| 42 | Cashback mode | **MVP-P2** | `loyalty_mode="cashback"`. Earn `cashback_percent` of `final_price`. Redeem up to `cashback_max_redeem_percent` of the price. The mode can be switched only while every balance is 0. |
| 43 | Stamp card | **MVP** | The default mode. Shown as ✂️✂️✂️▫️▫️▫️. |
| 44 | Redeem at booking (reserved) | **MVP** | The booking snapshots `bonus_cost`. Available = balance − reserved (the sum of `bonus_cost` over active bookings). Debited only on COMPLETED. Completion is never blocked (critic A5). |
| 45 | Welcome bonus | **MVP** | Credited on the client's first COMPLETED visit (reason `welcome`), never on sign-up, so it cannot be farmed. |
| 46 | Referral link | **MVP-P2** | `/start ref_<ref_code>` with an opaque 8-character code. Self-referral is rejected. Both sides are credited when the friend's first visit is COMPLETED and not auto-completed. |
| 47 | Tiers | later | |
| 48 | Manual adjustment | **MVP** | Admin only, comment required, audited, balance never below 0. |
| 49 | Bonus expiry | later | |
| 50 | Deposits | later | |
| 51 | Tips in Stars | skip | |
| 52 | Gift certificates | later | |
| 53 | Off-peak discounts | later | |
| 54 | Review request | **MVP-P2** (rating only) | Kind `rev` at `end + 2h`. ⭐1–5. A rating of 3 or less goes privately to the admins. Text and photo reviews are v1.1. |
| 55 | Rating on barber card | v1.1 | |
| 56 | Maps review link for 4–5★ | v1.1 | |
| 57 | Barber card with photo and bio | **MVP** | Portfolio album is v1.1. |
| 58 | Price list | **MVP** | |
| 59 | Venue, map buttons | **MVP** | `send_venue` only when lat/lon are set (critic E8). URL buttons for Yandex, Google and 2GIS come from settings. |
| 60 | Hours generated from DB | **MVP** | `schedule.get_shop_week_hours()` gives the union over active barbers. |
| 61 | Editable welcome text | v1.1 | |
| 62 | AI FAQ | skip | A static FAQ may come later. |
| 63 | Ask a human | v1.1 | |
| 64 | Channel gate | skip | |
| 65 | Own-contact verification | **MVP** | A verified phone is **required** for bot bookings (critic D1). Typed phones are allowed only for barber walk-ins. |
| 66 | Booking limits | **MVP** | Counted per user and per phone: `max_active_bookings`=3 and `max_requests_per_day`=5. The "1 per barber per day" rule was dropped (critic E5). |
| 67 | Cancel deadline | **MVP** | Flag policy: a late cancel of a CONFIRMED booking is allowed and counted. |
| 68 | No-show → approval needed | **MVP** | This is the requirements' soft-block (3 no-shows in 90 days). No deposit. |
| 69 | Ban / shadowban | **MVP** (ban with guard rails), v1.1 (shadowban) | Blocking cancels the user's active bookings. Admins cannot block themselves or staff. |
| 70 | Captcha | skip | |
| 71 | Throttling | **MVP** | Messages and callbacks, TTL buckets (critic H3). |
| 72 | Staff role reset | later | |
| 73 | Consent screen with version | **MVP** | Shown on the first `/start` and again when `CONSENT_VERSION` changes. Includes `/delete_me` (critic H1). |
| 74 | Admin CRUD | **MVP** | Hide, never delete, anything that has bookings. |
| 75 | Price snapshot | **MVP** | |
| 76 | Bookings list | **MVP** | Filter by day, barber and status, 10 per page. Admin phone search is audited. |
| 77 | Stats | **MVP** | Plus top services. |
| 78 | Advanced analytics | v1.1 | MVP stores `users.acq_source` from `/start src_<tag>` so it can be reported later. |
| 79 | Broadcast | v1.1 | |
| 80 | CSV export | v1.1 | |
| 81 | Client notes | v1.1 | The MVP admin profile already shows history, no-shows and bonus. |
| 82 | Sheets/Calendar mirror | later | |
| 83 | Admin/reception group | v1.1 | Covers critic F2. In MVP, admins get the fallback decision messages. |
| 84 | Payroll | later | |
| 85 | Inventory | skip | |
| 86 | Waitlist | v1.1 | |
| 87 | Multi-language | **MVP** (uz Latin, P1) + **MVP-P2** (ru) | Key-parity test. Uzbek Cyrillic is v1.1. |
| 88 | Localised command menu | **MVP** | |
| 89 | Error keys | **MVP** | `ServiceError.code` → `t(lang, "err." + code, **ctx)`. |
| 90 | Private-chat only | **MVP** | Outer middleware drops non-private updates. The bot leaves any group it is added to. |
| 91 | Fake Bot API tests | v1.1 | |
| 92 | Seed / demo data | **MVP** | Only with `--demo`, and refused on a non-empty DB (critic G3). |
| 93 | Backup | **MVP** | `tools/backup.sh` uses `sqlite3 .backup`, keeps 14 copies, and prints the restore command. |
| 94 | No auto-migrations in production | **MVP** | Alembic baseline. `python -m app.db.migrate` is a separate step. `main.py` refuses to start if the schema is not at head. |
| 95 | Mini App | skip | |
| 96 | Multi-tenant | skip | `shop_id` columns are kept. |
