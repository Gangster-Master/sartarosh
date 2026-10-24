"""All outgoing pushes (ARCHITECTURE.md §6.4).

Rules: never send inside a transaction (each DB touch is its own short
``read_tx``/``write_tx``); every text uses the *recipient's* language; global
pacing (≤20 concurrent, ≤25 msg/s); ``RetryAfter`` → sleep and retry once;
a barber who blocked the bot is marked unreachable and the admins are told;
a client who blocked the bot is only logged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.callbacks import BookingCb, LinkCb, QueueDayCb
from app.bot.i18n import fmt_dt, fmt_price, fmt_stamps, fmt_time, h, norm_lang, t
from app.bot.keyboards import barber as bkb
from app.bot.keyboards import client as ckb
from app.bot.keyboards.common import btn, kb, menu_for
from app.bot.screen import clip
from app.bot.views import booking_lines, reason_label
from app.db.enums import (
    Awaiting,
    BookingSource,
    BookingStatus,
    CancelledBy,
    LoyaltyMode,
    NotificationKind,
)
from app.db.uow import read_tx, write_tx
from app.services import barbers as barbers_svc
from app.services import bonus, bookings, clock, notifications, schedule, settings, users

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_CONCURRENCY = 20
MAX_PER_SECOND = 25
MAX_INLINE_RETRY_S = 5  # a longer RetryAfter is not waited out inline [D-7]


def _guard(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
    """A failing push must never break the caller (the DB work is already committed)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T | None:
        try:
            return await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("notifier.%s failed", fn.__name__)
            return None

    return wrapper


class Notifier:
    def __init__(self, bot: Bot, sm: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.sm = sm
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._sent: deque[float] = deque()
        self._pace_lock = asyncio.Lock()
        # seconds of the last RetryAfter too long to wait inline; the outbox reschedules
        self.flood_wait = 0.0

    # ------------------------------------------------------------ transport

    async def _pace(self) -> None:
        async with self._pace_lock:
            while True:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= 1.0:
                    self._sent.popleft()
                if len(self._sent) < MAX_PER_SECOND:
                    self._sent.append(now)
                    return
                await asyncio.sleep(1.0 - (now - self._sent[0]) + 0.01)

    async def _api(self, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._sem:
            await self._pace()
            try:
                return await factory()
            except TelegramRetryAfter as e:
                if e.retry_after > MAX_INLINE_RETRY_S:
                    self.flood_wait = max(self.flood_wait, float(e.retry_after))
                    raise
                wait = e.retry_after
        # a short flood wait: sleep without holding a concurrency slot, then retry once
        await asyncio.sleep(wait + 0.5)
        async with self._sem:
            await self._pace()
            return await factory()

    async def send(
        self,
        chat_id: int | None,
        text: str,
        markup: InlineKeyboardMarkup | Any | None = None,
        *,
        barber_id: int | None = None,
    ) -> Message | None:
        if not chat_id or chat_id < 0:
            return None
        text = clip(text)
        try:
            return await self._api(
                lambda: self.bot.send_message(chat_id, text, reply_markup=markup)
            )
        except TelegramForbiddenError:
            if barber_id is not None:
                await self._barber_unreachable(barber_id)
            else:
                log.info("chat %s blocked the bot", chat_id)
        except TelegramAPIError as e:
            log.warning("send to %s failed: %s", chat_id, e)
        return None

    async def edit(
        self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        text = clip(text)
        try:
            await self._api(
                lambda: self.bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
            )
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "not modified" in msg or "not found" in msg or "can't be edited" in msg:
                return
            if "there is no text" in msg:  # a photo message: edit the caption instead
                try:
                    await self._api(
                        lambda: self.bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=text[:1024],
                            reply_markup=markup,
                        )
                    )
                except TelegramAPIError:
                    pass
                return
            log.debug("edit %s/%s failed: %s", chat_id, message_id, e)
        except TelegramAPIError as e:
            log.debug("edit %s/%s failed: %s", chat_id, message_id, e)

    async def _barber_unreachable(self, barber_id: int) -> None:
        async with write_tx(self.sm) as s:
            changed = await barbers_svc.set_reachable(s, barber_id=barber_id, reachable=False)
            b = await barbers_svc.get_barber(s, barber_id)
        if not changed:  # already known: the admins were told the first time
            return
        log.warning("barber %s blocked the bot: marked unreachable", barber_id)
        await self.alert_admins("admin.alert.barber_unreachable", name=h(b.display_name))

    # ------------------------------------------------------------ lookups

    async def _lang_of(self, tg_id: int | None) -> str:
        if not tg_id:
            return "uz"
        async with read_tx(self.sm) as s:
            ctx = await users.load_context(s, tg_id=tg_id)
        return norm_lang(ctx.user.lang) if ctx is not None else "uz"

    async def _admins(self) -> list[tuple[int, str]]:
        async with read_tx(self.sm) as s:
            rows = await users.list_admin_chat_ids(s)
        return [(tg, norm_lang(lang)) for tg, lang in rows]

    async def _shop(self) -> Any:
        async with read_tx(self.sm) as s:
            return await settings.get_shop_settings(s)

    def _card(self, b: Any, lang: str, *, staff: bool) -> str:
        return "\n".join(booking_lines(b, lang, staff=staff, show_status=False))

    async def _decision_text(self, b: Any, lang: str, *, header_key: str, role: str) -> str:
        stats = None
        if b.client_id:
            async with read_tx(self.sm) as s:
                stats = await users.get_client_stats(s, b.client_id)
        lines = [t(lang, header_key), ""]
        lines.append(t(lang, "card.client_name", name=h(b.client_name)))
        lines.append(t(lang, "card.phone", phone=h(b.client_phone) if b.client_phone else "—"))
        if b.service_name:
            lines.append(t(lang, "card.service", name=h(b.service_name), min=b.duration_min))
        lines.append(t(lang, "card.when", when=fmt_dt(b.start_at, lang)))
        if role == "admin":
            lines.append(t(lang, "card.barber", name=h(b.barber_name)))
        if b.bonus_cost > 0:
            lines.append(t(lang, "card.bonus_used", price=fmt_price(b.final_price_uzs, lang)))
        if stats is not None:
            lines.append(
                t(lang, "card.stats", visits=stats.total_completed, noshow=stats.no_show_count)
            )
            if stats.is_new:
                lines.append(t(lang, "card.new_client"))
        if b.comment:
            lines.append(t(lang, "card.comment", text=h(b.comment)))
        if b.expire_at:
            lines.append(t(lang, "card.expire", when=fmt_dt(b.expire_at, lang)))
        return "\n".join(lines)

    async def _send_decisions(self, b: Any, targets: list[Any], *, header_key: str) -> int:
        sent = 0
        for tg in targets:
            lang = norm_lang(tg.lang)
            text = await self._decision_text(b, lang, header_key=header_key, role=tg.role)
            msg = await self.send(
                tg.chat_id,
                text,
                bkb.decision_kb(b.id, lang),
                barber_id=b.barber_id if tg.role == "barber" else None,
            )
            if msg is None:
                continue
            sent += 1
            async with write_tx(self.sm) as s:
                await bookings.add_decision_message(
                    s, booking_id=b.id, chat_id=msg.chat.id, message_id=msg.message_id, role=tg.role
                )
        if sent == 0 and targets:
            # the barber may just have been marked unreachable: fall back to admins
            admin_targets = [x for x in targets if x.role == "admin"]
            if not admin_targets:
                for tg_id, lang in await self._admins():
                    msg = await self.send(
                        tg_id,
                        await self._decision_text(b, lang, header_key=header_key, role="admin"),
                        bkb.decision_kb(b.id, lang),
                    )
                    if msg is not None:
                        sent += 1
                        async with write_tx(self.sm) as s:
                            await bookings.add_decision_message(
                                s,
                                booking_id=b.id,
                                chat_id=msg.chat.id,
                                message_id=msg.message_id,
                                role="admin",
                            )
        return sent

    async def _decision_messages(self, booking_id: int) -> list[tuple[int, int, str]]:
        async with read_tx(self.sm) as s:
            return await bookings.list_decision_messages(s, booking_id)

    async def _edit_decisions(
        self,
        b: Any,
        outcome_key: str,
        *,
        skip: tuple[int, int] | None = None,
        late_accept: bool = False,
        **kw: Any,
    ) -> list[int]:
        """Edit every stored decision message to the outcome, without decision buttons.
        Returns chat ids. ``skip`` = (chat_id, message_id) the handler has just redrawn
        itself; ``late_accept`` keeps an accept button for the grace period [C-B5]."""
        chats = []
        for chat_id, message_id, _role in await self._decision_messages(b.id):
            if skip is not None and (chat_id, message_id) == skip:
                chats.append(chat_id)
                continue
            lang = await self._lang_of(chat_id)
            text = (
                t(lang, outcome_key, **_localize(kw, lang))
                + "\n\n"
                + self._card(b, lang, staff=True)
            )
            rows = [[btn(t(lang, "barber.open_card"), BookingCb(a="card", id=b.id))]]
            if late_accept:
                rows.insert(0, [btn(t(lang, "barber.late_accept"), BookingCb(a="acc", id=b.id))])
            await self.edit(chat_id, message_id, text, kb(rows))
            chats.append(chat_id)
        return chats

    async def _send_venue(self, chat_id: int | None, shop: Any) -> None:
        if not chat_id or shop.latitude is None or shop.longitude is None:
            return
        title = shop.shop_name or "💈"
        address = shop.address or " "
        try:
            await self._api(
                lambda: self.bot.send_venue(
                    chat_id=chat_id,
                    latitude=float(shop.latitude),
                    longitude=float(shop.longitude),
                    title=title,
                    address=address,
                )
            )
        except TelegramAPIError as e:
            log.debug("send_venue failed: %s", e)

    def _address_line(self, shop: Any, lang: str) -> str:
        parts = []
        if shop.address:
            parts.append(t(lang, "notify.address", address=h(shop.address)))
        if shop.contact_phone:
            parts.append(t(lang, "notify.contact", phone=h(shop.contact_phone)))
        return "\n".join(parts)

    def _client_targetable(self, b: Any) -> bool:
        return bool(b.client_tg_id and b.client_tg_id > 0 and b.source == BookingSource.BOT)

    # ------------------------------------------------------------ booking lifecycle

    @_guard
    async def booking_created(self, res: Any) -> None:
        b = res.booking
        if b.status == BookingStatus.CONFIRMED:
            shop = await self._shop()
            await self._send_venue(b.client_tg_id, shop)
            if b.barber_tg_id:
                lang = await self._lang_of(b.barber_tg_id)
                text = (
                    t(lang, "notify.barber_new_confirmed")
                    + "\n\n"
                    + self._card(b, lang, staff=True)
                )
                await self.send(
                    b.barber_tg_id,
                    text,
                    kb([[btn(t(lang, "barber.open_card"), BookingCb(a="card", id=b.id))]]),
                    barber_id=b.barber_id,
                )
            return
        if res.decision_targets:
            sent = await self._send_decisions(
                b, res.decision_targets, header_key="notify.decision_new"
            )
            if sent == 0:  # nobody got it (Telegram down?): the outbox retries later
                async with write_tx(self.sm) as s:
                    await notifications.schedule_decision_retry(s, booking_id=b.id)
        else:
            log.warning("booking %s is pending without decision targets", b.id)
            await self.alert_admins("admin.alert.no_targets", id=b.id)

    @_guard
    async def booking_confirmed(self, b: Any, *, skip: tuple[int, int] | None = None) -> None:
        await self._edit_decisions(b, "notify.outcome.confirmed", skip=skip, name=h(b.barber_name))
        if not self._client_targetable(b):
            return
        lang = norm_lang(b.client_lang)
        shop = await self._shop()
        text = t(lang, "notify.client_confirmed") + "\n\n" + self._card(b, lang, staff=False)
        addr = self._address_line(shop, lang)
        if addr:
            text += "\n\n" + addr
        await self.send(b.client_tg_id, text, ckb.result_kb(b, lang))
        await self._send_venue(b.client_tg_id, shop)

    @_guard
    async def booking_rejected(self, b: Any) -> None:
        await self._edit_decisions(b, "notify.outcome.rejected", name=h(b.barber_name))
        if not self._client_targetable(b):
            return
        lang = norm_lang(b.client_lang)
        text = t(
            lang, "notify.client_rejected", when=fmt_dt(b.start_at, lang), name=h(b.barber_name)
        )
        if b.cancel_reason:
            text += "\n" + t(lang, "card.reason", reason=reason_label(lang, b.cancel_reason))
        await self.send(b.client_tg_id, text, ckb.rebook_kb(b.barber_id, b.service_id, lang))

    @_guard
    async def booking_proposed(self, r: Any) -> None:
        b = r.booking
        await self._edit_decisions(b, "notify.outcome.proposed", when=b.start_at)
        if not self._client_targetable(b):
            return
        lang = norm_lang(b.client_lang)
        text = t(
            lang, "notify.client_proposal", name=h(b.barber_name), when=fmt_dt(b.start_at, lang)
        )
        if b.original_start_at:
            text += "\n" + t(lang, "card.original", when=fmt_dt(b.original_start_at, lang))
        if b.expire_at:
            text += "\n" + t(lang, "notify.reply_until", when=fmt_time(b.expire_at))
        await self.send(b.client_tg_id, text, ckb.proposal_answer_kb(b.id, lang))

    @_guard
    async def proposal_answered(self, b: Any) -> None:
        accepted = b.status == BookingStatus.CONFIRMED
        key = "notify.outcome.proposal_accepted" if accepted else "notify.outcome.proposal_declined"
        chats = set(await self._edit_decisions(b, key))
        recipients: dict[int, int | None] = {c: None for c in chats}
        if b.barber_tg_id:
            recipients[b.barber_tg_id] = b.barber_id
        for chat_id, barber_id in recipients.items():
            lang = await self._lang_of(chat_id)
            text = t(lang, key) + "\n\n" + self._card(b, lang, staff=True)
            await self.send(chat_id, text, barber_id=barber_id)

    @_guard
    async def booking_expired(self, b: Any) -> None:
        if b.awaiting == Awaiting.CLIENT:
            # the client did not answer the barber's proposed time
            await self._edit_decisions(b, "notify.outcome.proposal_expired")
            if self._client_targetable(b):
                lang = norm_lang(b.client_lang)
                text = t(
                    lang,
                    "notify.client_proposal_expired",
                    when=fmt_dt(b.start_at, lang),
                    name=h(b.barber_name),
                )
                await self.send(
                    b.client_tg_id, text, ckb.rebook_kb(b.barber_id, b.service_id, lang)
                )
            if b.barber_tg_id:
                lang = await self._lang_of(b.barber_tg_id)
                text = (
                    t(lang, "notify.outcome.proposal_expired")
                    + "\n\n"
                    + self._card(b, lang, staff=True)
                )
                await self.send(b.barber_tg_id, text, barber_id=b.barber_id)
            return
        await self._edit_decisions(b, "notify.outcome.expired", late_accept=True)
        if self._client_targetable(b):
            lang = norm_lang(b.client_lang)
            text = t(
                lang, "notify.client_expired", when=fmt_dt(b.start_at, lang), name=h(b.barber_name)
            )
            await self.send(b.client_tg_id, text, ckb.rebook_kb(b.barber_id, b.service_id, lang))
        await self.alert_admins(
            "admin.alert.expired",
            id=b.id,
            name=h(b.barber_name),
            client=h(b.client_name),
            when=b.start_at,
        )

    @_guard
    async def booking_cancelled(self, b: Any, *, by_client: bool) -> None:
        if by_client:
            await self._edit_decisions(b, "notify.outcome.client_cancelled")
            if b.barber_tg_id:
                lang = await self._lang_of(b.barber_tg_id)
                key = (
                    "notify.barber_client_cancelled_late"
                    if b.late_cancel
                    else "notify.barber_client_cancelled"
                )
                text = t(lang, key) + "\n\n" + self._card(b, lang, staff=True)
                await self.send(b.barber_tg_id, text, barber_id=b.barber_id)
            return
        await self._edit_decisions(b, "notify.outcome.staff_cancelled")
        if self._client_targetable(b):
            lang = norm_lang(b.client_lang)
            text = t(
                lang,
                "notify.client_cancelled",
                when=fmt_dt(b.start_at, lang),
                name=h(b.barber_name),
            )
            if b.cancel_reason and b.cancel_reason not in ("blocked", "deleted"):
                text += "\n" + t(lang, "card.reason", reason=reason_label(lang, b.cancel_reason))
            await self.send(b.client_tg_id, text, ckb.rebook_kb(b.barber_id, b.service_id, lang))
        if b.cancelled_by == CancelledBy.ADMIN and b.barber_tg_id:
            lang = await self._lang_of(b.barber_tg_id)
            text = (
                t(lang, "notify.barber_admin_cancelled") + "\n\n" + self._card(b, lang, staff=True)
            )
            await self.send(b.barber_tg_id, text, barber_id=b.barber_id)

    @_guard
    async def booking_completed(self, r: Any) -> None:
        b = r.booking
        if r.bonus_failed and r.booking.auto_completed and b.barber_tg_id:
            lang = await self._lang_of(b.barber_tg_id)
            await self.send(
                b.barber_tg_id, t(lang, "barber.bonus_failed", id=b.id), barber_id=b.barber_id
            )
        if r.referral_credited_to:
            await self._referral_credited(r.referral_credited_to)
        if b.auto_completed or not self._client_targetable(b):
            return  # an auto-completion thanks the client by day via the outbox (thx)
        lang = norm_lang(b.client_lang)
        lines = [t(lang, "notify.client_completed", name=h(b.barber_name))]
        lines.extend(
            await self._bonus_lines(b.client_id, lang, earned=r.earned, welcome=r.welcome_credited)
        )
        await self.send(
            b.client_tg_id, "\n".join(lines), ckb.rebook_kb(b.barber_id, b.service_id, lang)
        )

    async def _bonus_lines(
        self, user_id: int | None, lang: str, *, earned: int, welcome: int
    ) -> list[str]:
        if not user_id:
            return []
        async with read_tx(self.sm) as s:
            st = await bonus.get_bonus_status(s, user_id, recent_limit=0)
        if not st.enabled:
            return []
        lines = []
        if st.mode == LoyaltyMode.STAMPS:
            if earned:
                lines.append(t(lang, "notify.stamp_earned", n=earned))
            if welcome:
                lines.append(t(lang, "notify.welcome_bonus", n=welcome))
            lines.append(f"{fmt_stamps(st.progress, st.required)} {st.progress}/{st.required}")
            if st.can_redeem:
                lines.append(t(lang, "bonus.ready"))
        else:
            if earned:
                lines.append(t(lang, "notify.cashback_earned", amount=fmt_price(earned, lang)))
            if welcome:
                lines.append(t(lang, "notify.welcome_cashback", amount=fmt_price(welcome, lang)))
            lines.append(t(lang, "bonus.cash_balance", amount=fmt_price(st.available, lang)))
        return lines

    async def _referral_credited(self, referrer_user_id: int) -> None:
        async with read_tx(self.sm) as s:
            u = await users.get_user(s, referrer_user_id)
        lang = norm_lang(u.lang)
        await self.send(u.tg_id, t(lang, "notify.referral_credited"))

    @_guard
    async def booking_no_show(self, r: Any) -> None:
        b = r.booking
        if not self._client_targetable(b):
            return
        lang = norm_lang(b.client_lang)
        await self.send(
            b.client_tg_id,
            t(lang, "notify.client_no_show", when=fmt_dt(b.start_at, lang)),
            ckb.rebook_kb(b.barber_id, b.service_id, lang),
        )

    @_guard
    async def time_off_cancellations(self, items: list[Any]) -> None:
        for b in items:
            await self.booking_cancelled(b, by_client=False)

    # ------------------------------------------------------------ outbox

    @_guard
    async def due_notification(self, n: Any) -> bool:
        b = n.booking
        kind = n.kind
        if kind == NotificationKind.PENDING_REPING:
            return await self._send_decisions(b, n.targets, header_key="notify.decision_reping") > 0
        if not b.client_tg_id or b.client_tg_id < 0:
            return False
        lang = norm_lang(b.client_lang)
        if kind == NotificationKind.REMINDER_24H:
            text = (
                t(lang, "notify.r24", when=fmt_dt(b.start_at, lang))
                + "\n\n"
                + self._card(b, lang, staff=False)
            )
            markup = ckb.reminder24_kb(b.id, lang)
        elif kind == NotificationKind.REMINDER_2H:
            shop = await self._shop()
            text = t(lang, "notify.r2", time=fmt_time(b.start_at), name=h(b.barber_name))
            addr = self._address_line(shop, lang)
            if addr:
                text += "\n\n" + addr
            markup = None
        elif kind == NotificationKind.RETURN:
            text = t(lang, "notify.ret", name=h(b.barber_name))
            markup = ckb.rebook_kb(b.barber_id, b.service_id, lang)
        elif kind == NotificationKind.REVIEW:
            text = t(lang, "notify.rev", name=h(b.barber_name))
            markup = ckb.rating_kb(b.id)
        elif kind == NotificationKind.MANUAL_VISIT_CONFIRM:
            if clock.to_local(b.start_at).date() == clock.local_today():
                text = t(lang, "notify.mcf", time=fmt_time(b.start_at), name=h(b.barber_name))
            else:  # sent the next morning (e.g. after auto-completion): name the day
                text = t(
                    lang, "notify.mcf_day", when=fmt_dt(b.start_at, lang), name=h(b.barber_name)
                )
            markup = ckb.visit_confirm_kb(b.id, lang)
        elif kind == NotificationKind.COMPLETED_THANKS:
            lines = [t(lang, "notify.client_completed", name=h(b.barber_name))]
            lines.extend(await self._bonus_lines(b.client_id, lang, earned=0, welcome=0))
            text = "\n".join(lines)
            markup = ckb.rebook_kb(b.barber_id, b.service_id, lang)
        else:
            log.warning("unknown notification kind %s", kind)
            return False
        return await self.send(b.client_tg_id, text, markup) is not None

    # ------------------------------------------------------------ daily jobs

    @_guard
    async def morning_summary(self, day: date) -> None:
        async with read_tx(self.sm) as s:
            per_barber = await bookings.bookings_for_daily_summary(s, day=day)
            pending = await bookings.list_pending_digest(s, day=day)
            all_barbers = {b.id: b for b in await barbers_svc.list_barbers(s, public_only=False)}
            # barbers working today get the summary even with no bookings (morning_empty)
            working = [
                b.id
                for b in all_barbers.values()
                if b.is_public and await schedule.barber_windows(s, b.id, day, day)
            ]
        for barber_id in list(per_barber) + [x for x in working if x not in per_barber]:
            items = per_barber.get(barber_id, [])
            barber = all_barbers.get(barber_id)
            if barber is None or not barber.tg_id or not barber.is_active:
                continue
            lang = await self._lang_of(barber.tg_id)
            if items:
                # the header counts confirmed visits; unanswered requests are marked ⏳
                confirmed = sum(1 for b in items if b.status == BookingStatus.CONFIRMED)
                lines = [t(lang, "notify.morning", n=confirmed), ""]
                for b in sorted(items, key=lambda x: x.start_at):
                    svc = f" · {h(b.service_name)}" if b.service_name else ""
                    phone = f" · {h(b.client_phone)}" if b.client_phone else ""
                    mark = "⏳ " if b.status == BookingStatus.PENDING else ""
                    lines.append(f"{mark}{fmt_time(b.start_at)} {h(b.client_name)}{phone}{svc}")
                text = "\n".join(lines)
            else:
                text = t(lang, "notify.morning_empty")
            await self.send(
                barber.tg_id,
                text,
                kb([[btn(t(lang, "menu.b_today"), QueueDayCb(d=day.strftime("%Y%m%d")))]]),
                barber_id=barber.id,
            )
        admin_items = []
        for item in pending:
            await self._send_decisions(
                item.booking, item.targets, header_key="notify.decision_morning"
            )
            if any(x.role == "admin" for x in item.targets):
                admin_items.append(item.booking)
        if admin_items:
            for tg_id, lang in await self._admins():
                lines = [t(lang, "admin.alert.pending_digest", n=len(admin_items))]
                for b in admin_items:
                    lines.append(
                        f"• {fmt_time(b.start_at)} {h(b.barber_name)} — {h(b.client_name)}"
                    )
                await self.send(tg_id, "\n".join(lines))

    @_guard
    async def nightly_digest(self) -> None:
        async with read_tx(self.sm) as s:
            items = await bookings.list_unresolved(s)
            shop = await settings.get_shop_settings(s)
            all_barbers = {b.id: b for b in await barbers_svc.list_barbers(s, public_only=False)}
        by_barber: dict[int, list[Any]] = {}
        for b in items:
            by_barber.setdefault(b.barber_id, []).append(b)
        admins = None
        for barber_id, rows in by_barber.items():
            barber = all_barbers.get(barber_id)
            recipients: list[tuple[int, str, int | None]] = []
            if (
                barber is not None
                and barber.tg_id
                and barber.is_reachable
                and barber.is_active  # a deactivated barber's taps are refused: ask admins
            ):
                recipients.append((barber.tg_id, await self._lang_of(barber.tg_id), barber.id))
            else:
                if admins is None:
                    admins = await self._admins()
                recipients.extend((tg, lang, None) for tg, lang in admins)
            for chat_id, lang, bid in recipients:
                lines = [
                    t(lang, "notify.nightly", n=len(rows), hours=shop.auto_complete_after_hours),
                    "",
                ]
                buttons = []
                for b in sorted(rows, key=lambda x: x.start_at):
                    lines.append(f"• {fmt_dt(b.start_at, lang)} — {h(b.client_name)}")
                    label = f"{fmt_time(b.start_at)} {b.client_name[:14]}"
                    buttons.append(
                        [
                            btn(f"✅ {label}", BookingCb(a="dd", id=b.id)),
                            btn(f"🚫 {label}", BookingCb(a="dn", id=b.id)),
                        ]
                    )
                await self.send(chat_id, "\n".join(lines), kb(buttons[:40]), barber_id=bid)

    # ------------------------------------------------------------ staff / admin

    @_guard
    async def link_requested(self, req: Any) -> None:
        u = req.user
        uname = f"@{u.tg_username}" if u.tg_username else "—"
        for tg_id, lang in await self._admins():
            text = t(
                lang,
                "admin.link_request",
                user=h(u.display_name),
                username=h(uname),
                tg_id=u.tg_id,
                barber=h(req.barber.display_name),
            )
            markup = kb(
                [
                    [
                        btn(t(lang, "common.approve"), LinkCb(a="ok", b=req.barber.id, u=u.id)),
                        btn(t(lang, "common.deny"), LinkCb(a="no", b=req.barber.id, u=u.id)),
                    ]
                ]
            )
            await self.send(tg_id, text, markup)

    @_guard
    async def link_decided(self, barber: Any, user_tg_id: int, approved: bool) -> None:
        from app.bot.commands import set_user_commands

        async with read_tx(self.sm) as s:
            ctx = await users.load_context(s, tg_id=user_tg_id)
        lang = norm_lang(ctx.user.lang) if ctx is not None else "uz"
        if approved:
            roles = ctx.roles if ctx is not None else None
            await self.send(
                user_tg_id,
                t(lang, "barber.linked", name=h(barber.display_name)),
                menu_for(roles, lang, "b") if roles is not None else None,
            )
            await set_user_commands(self.bot, user_tg_id, lang, roles)
        else:
            await self.send(user_tg_id, t(lang, "barber.link_denied", name=h(barber.display_name)))

    @_guard
    async def low_rating(self, r: Any) -> None:
        async with read_tx(self.sm) as s:
            b = await bookings.get_booking(s, r.booking_id)
        for tg_id, lang in await self._admins():
            text = (
                t(lang, "admin.alert.low_rating", rating=r.rating)
                + "\n\n"
                + self._card(b, lang, staff=True)
            )
            await self.send(tg_id, text)

    @_guard
    async def manual_visit_denied(self, b: Any) -> None:
        for tg_id, lang in await self._admins():
            text = t(lang, "admin.alert.visit_denied") + "\n\n" + self._card(b, lang, staff=True)
            await self.send(tg_id, text)

    @_guard
    async def alert_admins(self, key: str, **kw: object) -> None:
        for tg_id, lang in await self._admins():
            await self.send(tg_id, t(lang, key, **_localize(kw, lang)))

    async def send_document_to_env_admins(self, path: Any, caption: str) -> None:
        from aiogram.types import FSInputFile

        from app.config import get_config

        for tg_id in get_config().admin_ids:
            try:
                await self._api(
                    lambda tg=tg_id: self.bot.send_document(tg, FSInputFile(path), caption=caption)
                )
            except TelegramAPIError as e:
                log.warning("backup send to %s failed: %s", tg_id, e)


def _localize(kw: dict[str, Any], lang: str) -> dict[str, Any]:
    """Datetime values are rendered in the recipient's language."""
    return {k: (fmt_dt(v, lang) if isinstance(v, datetime) else v) for k, v in kw.items()}
