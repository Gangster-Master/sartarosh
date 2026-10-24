"""Shared fixtures (ARCHITECTURE.md §10).

Environment is set BEFORE any ``app`` import [C3].
"""

from __future__ import annotations

import os

os.environ["BOT_TOKEN"] = "0:test"
os.environ["SHOP_TZ"] = "Asia/Tashkent"
os.environ["ADMIN_IDS"] = "1000"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ.pop("REDIS_URL", None)

from collections.abc import AsyncIterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.config import get_config  # noqa: E402

get_config.cache_clear()

from app.db.models import Base  # noqa: E402
from app.db.session import create_engine, create_sessionmaker  # noqa: E402
from tests.factories import World, build_world  # noqa: E402


async def _make_engine(url: str) -> AsyncEngine:
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine("sqlite+aiosqlite:///:memory:")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def file_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    yield eng
    await eng.dispose()


@pytest.fixture
def sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
def file_sm(file_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(file_engine)


@pytest_asyncio.fixture
async def world(sm: async_sessionmaker[AsyncSession]) -> World:
    return await build_world(sm)


@pytest_asyncio.fixture
async def file_world(file_sm: async_sessionmaker[AsyncSession]) -> World:
    return await build_world(file_sm)


@pytest.fixture(autouse=True)
def _no_swallowed_errors(caplog: pytest.LogCaptureFixture):  # type: ignore[no-untyped-def]
    """Handlers/notifier/scheduler swallow exceptions by design; tests must still see them."""
    import logging

    caplog.set_level(logging.ERROR)
    yield
    bad = [
        r
        for r in caplog.get_records("setup") + caplog.get_records("call")
        if r.levelno >= logging.ERROR
        and r.name.startswith(("app.bot.handlers.errors", "app.bot.notifier", "app.bot.scheduler"))
    ]
    assert not bad, "\n".join(f"{r.name}: {r.getMessage()} {r.exc_text or ''}" for r in bad)
