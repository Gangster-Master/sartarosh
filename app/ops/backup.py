"""SQLite online backup, keep N (ARCHITECTURE.md §4.15).

``python -m app.ops.backup`` writes ``<DATA_DIR>/backups/bot-YYYY-MM-DD.db``.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_config
from app.db.session import resolve_database_url
from app.services import clock


def backup_sqlite(
    db_path: Path, backup_dir: Path, *, keep: int = 14, now: datetime | None = None
) -> Path:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"bot-{clock.to_local(now).date().isoformat()}.db"
    tmp = target.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                raise RuntimeError(f"backup integrity_check failed: {ok}")
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)
    backups = sorted(backup_dir.glob("bot-*.db"))
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink(missing_ok=True)
    return target


def default_db_path() -> Path:
    url = make_url(resolve_database_url(None))
    if not url.drivername.startswith("sqlite") or not url.database:
        raise RuntimeError("backup supports SQLite file databases only")
    return Path(url.database)


def main() -> int:
    cfg = get_config()
    db = default_db_path()
    path = backup_sqlite(db, cfg.data_path / "backups")
    print(f"backup written: {path}")
    print(f"restore: stop the bot, then: rm -f {db}-wal {db}-shm && cp {path} {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
