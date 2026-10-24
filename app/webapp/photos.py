"""Barber photo proxy: Telegram file_id -> bytes with a small in-memory cache
(docs/WEBAPP.md §4.4, [J-B5]).

At most 30 entries of at most 1 MB each (larger files are served but not cached),
24 h TTL, failures cached for 10 min, one download per file id at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from io import BytesIO
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger("app.webapp")

MAX_ENTRIES = 30
MAX_BYTES = 1024 * 1024
TTL = 24 * 3600.0
NEGATIVE_TTL = 600.0


class PhotoCache:
    def __init__(self, bot: Bot | None) -> None:
        self.bot = bot
        self._data: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _cached(self, file_id: str, now: float) -> tuple[bool, bytes | None]:
        hit = self._data.get(file_id)
        if hit is None:
            return False, None
        exp, data = hit
        if exp <= now:
            del self._data[file_id]
            return False, None
        self._data.move_to_end(file_id)
        return True, data

    def _store(self, file_id: str, data: bytes | None, ttl: float, now: float) -> None:
        self._data[file_id] = (now + ttl, data)
        self._data.move_to_end(file_id)
        while len(self._data) > MAX_ENTRIES:
            self._data.popitem(last=False)

    async def _download(self, file_id: str) -> bytes:
        assert self.bot is not None
        file = await self.bot.get_file(file_id)
        buf = BytesIO()
        await self.bot.download(file, destination=buf)
        return buf.getvalue()

    async def get(self, file_id: str) -> bytes | None:
        """Photo bytes, or None (not available / Telegram failed)."""
        found, data = self._cached(file_id, time.monotonic())
        if found:
            return data
        if self.bot is None:
            return None
        lock = self._locks.get(file_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[file_id] = lock
        async with lock:
            found, data = self._cached(file_id, time.monotonic())
            if found:
                return data
            try:
                data = await self._download(file_id)
            except Exception as e:  # noqa: BLE001 - any Telegram/network failure -> 404
                log.warning("webapp: barber photo download failed: %s", type(e).__name__)
                self._store(file_id, None, NEGATIVE_TTL, time.monotonic())
                return None
            if not data:
                self._store(file_id, None, NEGATIVE_TTL, time.monotonic())
                return None
            if len(data) <= MAX_BYTES:
                self._store(file_id, data, TTL, time.monotonic())
            return data
