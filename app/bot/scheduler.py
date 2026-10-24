"""One interval job (``tick``) drives everything time-based (ARCHITECTURE.md §6.5).

There are no cron jobs [C4]: daily jobs run inside the tick, guarded by a marker
row so each runs at most once per local day and setting changes apply on the
next tick. Each step uses its own short transaction; Telegram I/O happens only
after the commit [A2][A4].
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.notifier import Notifier
from app.config import get_config
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, fsm_store, notifications, settings

log = logging.getLogger(__name__)

MORNING_GRACE = timedelta(hours=3)
PURGE_EVERY = 3600.0

_scheduler: AsyncIOScheduler | None = None


def _parse_hhmm(v: object, default: time) -> time:
    if isinstance(v, time):
        return v
    try:
        hh, mm = str(v).split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default


class Ticker:
    def __init__(self, bot: Bot, sm: async_sessionmaker[AsyncSession], notifier: Notifier) -> None:
        self.bot = bot
        self.sm = sm
        self.notifier = notifier
        self._last_purge = 0.0

    async def tick(self) -> None:
        # _daily before _auto_complete: the 23:00 digest asks the barber first (§4.10)
        for step in (self._expire, self._daily, self._auto_complete, self._outbox, self._purge):
            try:
                await step()
            except Exception:  # noqa: BLE001 - one failing step must not stop the others
                log.exception("tick step %s failed", step.__name__)

    async def _expire(self) -> None:
        async with write_tx(self.sm) as s:
            expired = await bookings.expire_due_pending(s)
        for b in expired:
            await self.notifier.booking_expired(b)

    async def _auto_complete(self) -> None:
        async with write_tx(self.sm) as s:
            done = await bookings.auto_complete_due(s)
        for r in done:
            await self.notifier.booking_completed(r)

    async def _outbox(self) -> None:
        async with write_tx(self.sm) as s:
            due = await notifications.claim_due(s, limit=50)
        for n in due:
            ok = False
            err = "not delivered"
            self.notifier.flood_wait = 0.0
            try:
                ok = bool(await self.notifier.due_notification(n))
            except Exception as e:  # noqa: BLE001
                err = repr(e)[:200]
                log.exception("due notification %s failed", n.id)
            async with write_tx(self.sm) as s:
                if ok:
                    await notifications.mark_sent(s, notification_id=n.id)
                elif self.notifier.flood_wait:  # Telegram asked to wait: try again later
                    await notifications.retry_later(
                        s, notification_id=n.id, seconds=self.notifier.flood_wait
                    )
                else:
                    await notifications.mark_failed(s, notification_id=n.id, error=err)

    async def _daily(self) -> None:
        async with read_tx(self.sm) as s:
            shop = await settings.get_shop_settings(s)
        now_local = clock.to_local(clock.now_utc())
        today = now_local.date()
        jobs = (
            ("morning", _parse_hhmm(shop.daily_summary_time, time(8, 0))),
            ("nightly", _parse_hhmm(shop.nightly_check_time, time(23, 0))),
            ("backup", _parse_hhmm(shop.backup_time, time(3, 30))),
        )
        for name, at in jobs:
            if now_local.time() < at:
                continue
            marker = f"_job.{name}"
            async with read_tx(self.sm) as s:
                last = await settings.get_marker(s, marker)
            if last == today.isoformat():
                continue
            # mark first, commit, then run: at most once a day even if the run crashes
            async with write_tx(self.sm) as s:
                await settings.set_marker(s, marker, today.isoformat())
            if name == "morning":
                scheduled = datetime.combine(today, at, tzinfo=now_local.tzinfo)
                if now_local - scheduled > MORNING_GRACE:
                    log.info("morning summary skipped (too late: %s)", now_local.strftime("%H:%M"))
                    continue
                await self.notifier.morning_summary(today)
            elif name == "nightly":
                await self.notifier.nightly_digest()
            elif name == "backup":
                await self._backup()

    async def _backup(self) -> None:
        cfg = get_config()
        try:
            from app.db.session import resolve_database_url
            from app.ops.backup import backup_sqlite
        except ImportError:
            log.warning("backup module not available")
            return
        url = make_url(resolve_database_url(cfg.database_url))
        if (
            not url.drivername.startswith("sqlite")
            or not url.database
            or url.database == ":memory:"
        ):
            log.info("backup skipped: not a file sqlite database")
            return
        db_path = Path(url.database)
        backup_dir = cfg.data_path / "backups"
        try:
            path = await asyncio.to_thread(backup_sqlite, db_path, backup_dir)
        except Exception:  # noqa: BLE001
            log.exception("backup failed")
            await self.notifier.alert_admins("admin.alert.backup_failed")
            return
        log.info("backup written: %s", path)
        try:
            size = Path(path).stat().st_size
        except OSError:
            return
        if size < 45 * 1024 * 1024:
            await self.notifier.send_document_to_env_admins(path, f"💾 {Path(path).name}")
        else:  # over the Bot API upload limit: the file stays on the server only [D-6]
            await self.notifier.alert_admins(
                "admin.alert.backup_too_big", size_mb=size // (1024 * 1024), name=Path(path).name
            )

    async def _purge(self) -> None:
        now = _time.monotonic()
        if now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        async with write_tx(self.sm) as s:
            n = await fsm_store.purge_older_than(s, days=7)
        if n:
            log.info("purged %s stale FSM rows", n)


def start(bot: Bot, sm: async_sessionmaker[AsyncSession], notifier: Notifier) -> AsyncIOScheduler:
    global _scheduler
    ticker = Ticker(bot, sm, notifier)
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        ticker.tick,
        "interval",
        seconds=max(5, get_config().scheduler_tick_seconds),
        max_instances=1,
        coalesce=True,
        id="tick",
        next_run_time=datetime.now(tz=clock.now_utc().tzinfo) + timedelta(seconds=3),
    )
    sched.start()
    _scheduler = sched
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
