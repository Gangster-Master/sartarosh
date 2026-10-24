"""Per-user rate limit covering messages and callbacks [H3].

Sliding 60 s window, ``throttle_per_minute`` events; buckets expire after 10
minutes of inactivity and the table never holds more than 10 000 users.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update

from app.bot.i18n import t

WINDOW = 60.0
IDLE_TTL = 600.0
MAX_ENTRIES = 10_000


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, per_minute: int = 30) -> None:
        self.limit = max(1, per_minute)
        self._buckets: OrderedDict[int, deque[float]] = OrderedDict()

    def _gc(self, now: float) -> None:
        while self._buckets:
            uid, q = next(iter(self._buckets.items()))
            if len(self._buckets) > MAX_ENTRIES or not q or now - q[-1] > IDLE_TTL:
                self._buckets.popitem(last=False)
            else:
                break

    def hit(self, user_id: int, now: float | None = None) -> bool:
        """Record one event; True when the user is still under the limit."""
        now = time.monotonic() if now is None else now
        q = self._buckets.get(user_id)
        if q is None:
            q = deque()
            self._buckets[user_id] = q
        self._buckets.move_to_end(user_id)
        while q and now - q[0] > WINDOW:
            q.popleft()
        q.append(now)
        self._gc(now)
        return len(q) <= self.limit

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or (
            event.message is None and event.callback_query is None
        ):
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or self.hit(user.id):
            return await handler(event, data)
        code = (user.language_code or "")[:2]
        lang = "ru" if code == "ru" else "uz"
        if event.callback_query is not None:
            try:
                await event.callback_query.answer(t(lang, "common.too_fast"), show_alert=False)
            except TelegramAPIError:
                pass
        elif self._first_overflow(user.id):
            # messages get one "too fast" notice per window, not one per message [H3]
            try:
                bot: Bot = data["bot"]
                await bot.send_message(user.id, t(lang, "common.too_fast"))
            except (TelegramAPIError, KeyError):
                pass
        return None

    def _first_overflow(self, user_id: int) -> bool:
        q = self._buckets.get(user_id)
        return q is not None and len(q) == self.limit + 1
