"""Schema migrations (ARCHITECTURE.md §3.5).

``python -m app.db.migrate`` runs ``alembic upgrade head``. The schema is created
only by Alembic [G7].
"""

from __future__ import annotations

import sys

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import PROJECT_ROOT
from app.db.session import resolve_database_url


def alembic_config(url: str | None = None) -> AlembicConfig:
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", resolve_database_url(url).replace("%", "%%"))
    return cfg


def upgrade_head(url: str | None = None) -> None:
    command.upgrade(alembic_config(url), "head")


def head_revisions() -> set[str]:
    return set(ScriptDirectory.from_config(alembic_config()).get_heads())


async def assert_schema_current(engine: AsyncEngine) -> None:
    """RuntimeError if the database is not at the Alembic head [#94]."""

    def _current(sync_conn) -> set[str]:  # type: ignore[no-untyped-def]
        return set(MigrationContext.configure(sync_conn).get_current_heads())

    async with engine.connect() as conn:
        current = await conn.run_sync(_current)
    if current != head_revisions():
        raise RuntimeError("schema not at head: run python -m app.db.migrate")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    url = argv[0] if argv else None
    upgrade_head(url)
    print(f"database at head: {resolve_database_url(url)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
