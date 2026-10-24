"""Async Alembic environment (render_as_batch for SQLite)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from app.db.models import Base
from app.db.session import create_engine, resolve_database_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return resolve_database_url(config.get_main_option("sqlalchemy.url") or None)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_engine(_url())
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_do_run)
            await conn.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from inside a running loop (assert/upgrade helpers)
        _do_run(connection)
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_run_async())
        return
    # Called from inside a running event loop (e.g. an async test): run in a worker thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run_async()).result()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
