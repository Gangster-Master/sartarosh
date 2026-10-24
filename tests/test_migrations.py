from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.db import migrate
from app.db.models import Base


def test_upgrade_head_and_no_diff(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    migrate.upgrade_head(url)
    sync = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with sync.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    sync.dispose()
    assert diff == []


def test_assert_schema_current(tmp_path: Path) -> None:
    from app.db.session import create_engine as create_async

    url = f"sqlite+aiosqlite:///{tmp_path / 'n.db'}"

    async def check() -> None:
        eng = create_async(url)
        try:
            try:
                await migrate.assert_schema_current(eng)
            except RuntimeError:
                pass
            else:
                raise AssertionError("empty db must not be current")
        finally:
            await eng.dispose()

    asyncio.run(check())
    migrate.upgrade_head(url)

    async def check2() -> None:
        eng = create_async(url)
        try:
            await migrate.assert_schema_current(eng)
        finally:
            await eng.dispose()

    asyncio.run(check2())
    assert command  # imported for parity with ARCH §10 wording
