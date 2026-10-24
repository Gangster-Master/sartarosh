"""In-memory sliding-window rate limiter (docs/WEBAPP.md §3.4, [J-A6])."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Hashable


class RateLimiter:
    """``hit(key, limit)`` -> True if allowed. At most ``max_keys`` keys; evicts the oldest key when full."""

    def __init__(self, *, window: float = 60.0, max_keys: int = 10_000) -> None:
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[Hashable, deque[float]] = {}

    def hit(self, key: Hashable, limit: int, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._hits.get(key)
        if q is None:
            while len(self._hits) >= self.max_keys:
                # dicts keep insertion order: drop the oldest key, never everyone's limits
                self._hits.pop(next(iter(self._hits)))
            q = self._hits[key] = deque()
        edge = now - self.window
        while q and q[0] <= edge:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._hits.clear()
