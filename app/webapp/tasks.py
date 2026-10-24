"""Tracked fire-and-forget notifier calls, drained on shutdown [J-D2]."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("app.webapp")

_PENDING: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.get_running_loop().create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def drain_pending_notifications(timeout: float = 10.0) -> None:
    """Wait for in-flight notifications (bounded) before the bot session closes."""
    if not _PENDING:
        return
    pending = list(_PENDING)
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("webapp: %d notification(s) still running at shutdown", len(_PENDING))
