"""Identical callback from the same user within 1 s is answered and dropped [#10]."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update

WINDOW = 1.0
MAX_ENTRIES = 10_000


class DebounceMiddleware(BaseMiddleware):
    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        self._seen: OrderedDict[tuple[int, str], float] = OrderedDict()

    def is_duplicate(self, user_id: int, payload: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        key = (user_id, payload)
        last = self._seen.get(key)
        self._seen[key] = now
        self._seen.move_to_end(key)
        while self._seen:
            k, ts = next(iter(self._seen.items()))
            if len(self._seen) > MAX_ENTRIES or now - ts > self.window * 5:
                self._seen.popitem(last=False)
            else:
                break
        return last is not None and now - last < self.window

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and event.callback_query is not None:
            call = event.callback_query
            if self.is_duplicate(call.from_user.id, call.data or ""):
                try:
                    await call.answer()
                except TelegramAPIError:
                    pass
                return None
        return await handler(event, data)
