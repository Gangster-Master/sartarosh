"""Per-booking / per-user asyncio locks and the Idempotency-Key replay map [J-A1, J-D1].

In-process only: ``bot.lock`` guarantees a single process, so this is enough.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any
from weakref import WeakValueDictionary

_BOOKING_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_USER_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _get(table: WeakValueDictionary[int, asyncio.Lock], key: int) -> asyncio.Lock:
    lock = table.get(key)
    if lock is None:
        lock = asyncio.Lock()
        table[key] = lock
    return lock


def booking_lock(booking_id: int) -> asyncio.Lock:
    """``async with booking_lock(id):`` serialises state changes of one booking."""
    return _get(_BOOKING_LOCKS, int(booking_id))


def user_lock(tg_id: int) -> asyncio.Lock:
    return _get(_USER_LOCKS, int(tg_id))


class IdempotencyStore:
    """(tg_id, key) -> (status, body) with a TTL; oldest entries go first when full."""

    def __init__(self, *, ttl: float = 600.0, max_entries: int = 10_000) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: OrderedDict[tuple[int, str], tuple[float, int, Any]] = OrderedDict()

    def _purge(self, now: float) -> None:
        while self._data:
            k, (exp, _s, _b) = next(iter(self._data.items()))
            if exp > now:
                break
            del self._data[k]

    def get(self, tg_id: int, key: str, *, now: float | None = None) -> tuple[int, Any] | None:
        now = time.monotonic() if now is None else now
        self._purge(now)
        hit = self._data.get((tg_id, key))
        if hit is None:
            return None
        return hit[1], hit[2]

    def put(
        self, tg_id: int, key: str, status: int, body: Any, *, now: float | None = None
    ) -> None:
        now = time.monotonic() if now is None else now
        self._purge(now)
        while len(self._data) >= self.max_entries:
            self._data.popitem(last=False)
        self._data[(tg_id, key)] = (now + self.ttl, status, body)
