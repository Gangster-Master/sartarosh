"""Engine and session factory (ARCHITECTURE.md §3.3).

SQLite runs with ``isolation_level=None`` on the driver and an explicit
``BEGIN <mode>`` emitted by a ``begin`` listener, so ``write_tx`` can take the
write lock up front with ``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_ROOT, get_config

SQLITE_BEGIN_OPTION = "sqlite_begin"


def resolve_database_url(raw: str | None) -> str:
    """Resolve relative SQLite paths against PROJECT_ROOT and create the parent dir."""
    raw = raw or get_config().database_url
    url = make_url(raw)
    if not url.drivername.startswith("sqlite"):
        return raw
    db = url.database
    if not db or db == ":memory:" or db.startswith("file:"):
        return raw
    path = Path(db)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return url.set(database=str(path)).render_as_string(hide_password=False)


def _is_memory(url_str: str) -> bool:
    url = make_url(url_str)
    return url.drivername.startswith("sqlite") and (
        not url.database or url.database == ":memory:" or "mode=memory" in str(url)
    )


def _install_sqlite_listeners(engine: AsyncEngine) -> None:
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        # Let SQLAlchemy (our "begin" listener) own transaction boundaries.
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    @event.listens_for(sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        mode = conn.get_execution_options().get(SQLITE_BEGIN_OPTION, "DEFERRED")
        if mode not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
            mode = "DEFERRED"
        conn.exec_driver_sql(f"BEGIN {mode}")


def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    cfg = get_config()
    resolved = resolve_database_url(url)
    kwargs: dict[str, Any] = {"echo": echo or cfg.db_echo}
    is_sqlite = make_url(resolved).drivername.startswith("sqlite")
    if is_sqlite and _is_memory(resolved):
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = _create_async_engine(resolved, **kwargs)
    if is_sqlite:
        _install_sqlite_listeners(engine)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=True)
