"""Transaction ownership (ARCHITECTURE.md §3.4).

Services never commit or roll back. Handlers and jobs wrap service calls in
``write_tx`` / ``read_tx`` and do network I/O only after the block exits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SQLITE_BEGIN_OPTION


@asynccontextmanager
async def write_tx(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """BEGIN IMMEDIATE on SQLite; commit on normal exit, rollback on ANY exception."""
    async with sm() as session:
        # Procure the connection now so the begin listener sees the option.
        await session.connection(execution_options={SQLITE_BEGIN_OPTION: "IMMEDIATE"})
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def read_tx(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Deferred BEGIN; always rolled back on exit (read-only by convention)."""
    async with sm() as session:
        await session.connection(execution_options={SQLITE_BEGIN_OPTION: "DEFERRED"})
        try:
            yield session
        finally:
            await session.rollback()
