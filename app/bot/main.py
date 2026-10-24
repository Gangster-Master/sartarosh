"""Entry point: ``python -m app.bot.main`` (ARCHITECTURE.md §5.2).

Startup order: config + logging + tz grid check → single-instance lock →
engine + schema check → setup token → Bot/storage → Dispatcher + middlewares →
routers + error handler → commands → scheduler → Mini App server (WEBAPP_ENABLED) +
menu button → polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import sys
from typing import IO

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import SimpleEventIsolation

from app.bot import scheduler
from app.bot.commands import set_default_commands, set_user_commands
from app.bot.fsm_storage import DbStorage
from app.bot.handlers import build_root_router, errors
from app.bot.i18n import norm_lang
from app.bot.menu_button import setup_menu_button
from app.bot.middlewares import (
    DebounceMiddleware,
    PrivateOnlyMiddleware,
    ThrottleMiddleware,
    UserMiddleware,
)
from app.bot.notifier import Notifier
from app.config import get_config
from app.db.migrate import assert_schema_current
from app.db.session import create_engine, create_sessionmaker
from app.db.uow import read_tx, write_tx
from app.services import barbers, clock, settings, users

log = logging.getLogger("app.bot")

EXIT_LOCKED = 3

_lock_fh: IO[str] | None = None  # held for the process lifetime once taken


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,  # alembic's fileConfig (run.py --migrate) may have set root to WARNING
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _take_lock() -> IO[str]:
    """Single-instance lock; idempotent (run.py --migrate takes it before migrating)."""
    global _lock_fh
    if _lock_fh is not None:
        return _lock_fh
    cfg = get_config()
    path = cfg.data_path / "bot.lock"
    fh = open(path, "a+")  # noqa: SIM115 - kept open for the process lifetime
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another bot instance is already running (lock %s)", path)
        sys.exit(EXIT_LOCKED)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _lock_fh = fh
    return fh


def _release_lock(fh: IO[str]) -> None:
    global _lock_fh
    _lock_fh = None
    with contextlib.suppress(OSError):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _make_storage(sm: object) -> BaseStorage:
    cfg = get_config()
    if cfg.redis_url:
        from aiogram.fsm.storage.redis import RedisStorage

        log.info("FSM storage: Redis")
        return RedisStorage.from_url(cfg.redis_url)
    log.info("FSM storage: database")
    return DbStorage(sm)  # type: ignore[arg-type]


async def _refresh_staff_commands(bot: Bot, sm: object) -> None:
    """Per-chat command lists for barbers and admins [#88]."""
    async with read_tx(sm) as s:  # type: ignore[arg-type]
        admin_rows = await users.list_admin_chat_ids(s)
        staff = [
            (b.tg_id, b.user_id)
            for b in await barbers.list_barbers(s, public_only=False)
            if b.tg_id and b.is_active
        ]
        contexts = []
        seen = {tg_id for tg_id, _lang in admin_rows} | {tg_id for tg_id, _uid in staff}
        for tg_id in seen:
            ctx = await users.load_context(s, tg_id=tg_id)
            if ctx is not None:
                contexts.append(ctx)
    for ctx in contexts:
        await set_user_commands(bot, ctx.user.tg_id, norm_lang(ctx.user.lang), ctx.roles)


def allowed_updates(dp: Dispatcher) -> list[str]:
    """Handled update types plus ``my_chat_member``: PrivateOnlyMiddleware needs it to
    leave groups the bot is added to (no handler is registered for it)."""
    types = list(dp.resolve_used_update_types())
    if "my_chat_member" not in types:
        types.append("my_chat_member")
    return types


def build_dispatcher(
    bot: Bot, sm: object, storage: BaseStorage, *, throttle_per_minute: int | None = None
) -> tuple[Dispatcher, Notifier]:
    """Dispatcher with workflow data, outer middlewares (§5.2 step 7), routers and error handler.

    Separate from ``run()`` so smoke tests can drive the real stack with a fake bot session.
    """
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
    notifier = Notifier(bot, sm)  # type: ignore[arg-type]
    dp["sm"] = sm
    dp["notifier"] = notifier
    per_minute = throttle_per_minute or get_config().throttle_per_minute
    dp.update.outer_middleware(PrivateOnlyMiddleware())
    dp.update.outer_middleware(ThrottleMiddleware(per_minute))
    dp.update.outer_middleware(DebounceMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(build_root_router())
    dp.errors.register(errors.on_error)
    return dp, notifier


async def run() -> None:
    cfg = get_config()
    _setup_logging(cfg.log_level)
    if not hasattr(logging, cfg.log_level.upper()):
        log.warning("LOG_LEVEL=%r is not a logging level; using INFO", cfg.log_level)
    for bad in cfg.invalid_admin_ids:
        log.warning("ADMIN_IDS entry %r is not a Telegram id and is ignored", bad)
    if cfg.bot_token is None or not cfg.bot_token.get_secret_value():
        log.error("BOT_TOKEN is not set (see .env.example)")
        sys.exit(2)
    clock.assert_tz_on_grid()
    log.info(
        "shop timezone %s, local time %s",
        cfg.shop_tz,
        clock.to_local(clock.now_utc()).strftime("%Y-%m-%d %H:%M"),
    )

    lock = _take_lock()
    engine = create_engine(echo=cfg.db_echo)
    try:
        await assert_schema_current(engine)
        sm = create_sessionmaker(engine)

        async with write_tx(sm) as s:
            setup_token = await settings.ensure_setup_token(s)

        bot = Bot(
            token=cfg.bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        try:
            me = await bot.me()
        except TelegramUnauthorizedError:
            await bot.session.close()
            log.error("BOT_TOKEN was rejected by Telegram (Unauthorized); check .env")
            sys.exit(2)
        except BaseException:
            await bot.session.close()
            raise
        log.info("bot @%s (id %s) starting", me.username, me.id)
        if setup_token:
            log.warning("No admin. Open https://t.me/%s?start=setup_%s", me.username, setup_token)

        storage = _make_storage(sm)
        dp, notifier = build_dispatcher(bot, sm, storage)

        await set_default_commands(bot)
        try:
            await _refresh_staff_commands(bot, sm)
        except Exception:  # noqa: BLE001 - cosmetic, never block startup
            log.exception("refreshing staff commands failed")

        scheduler.start(bot, sm, notifier)
        webapp = None
        if cfg.webapp_enabled:
            from app.webapp.server import create_app, start_webapp

            web_app = create_app(
                sm=sm,
                bot=bot,
                notifier=notifier,
                bot_token=cfg.bot_token.get_secret_value(),
                bot_username=me.username or "",
            )
            try:
                webapp = await start_webapp(web_app, host=cfg.webapp_host, port=cfg.webapp_port)
            except OSError as e:  # port in use etc.: the bot keeps working without the web app
                log.error(
                    "webapp could not start on %s:%s: %s", cfg.webapp_host, cfg.webapp_port, e
                )
        await setup_menu_button(bot, cfg.webapp_public_url if webapp else None)
        try:
            await dp.start_polling(bot, allowed_updates=allowed_updates(dp), handle_signals=True)
        finally:
            if webapp is not None:
                await webapp.stop()  # drains pending notifications before the session closes
            scheduler.shutdown()
            await storage.close()
            await bot.session.close()
    finally:
        await engine.dispose()
        _release_lock(lock)


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (None, 0):
            raise
    except RuntimeError as e:
        # e.g. assert_schema_current: "schema not at head: run python -m app.db.migrate"
        if str(e).startswith("schema not at head"):
            logging.getLogger("app.bot").error("%s", e)
        else:  # a real startup bug: keep the traceback
            logging.getLogger("app.bot").exception("startup failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
