from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from app.ops.backup import backup_sqlite
from tests.factories import NOW


def test_backup_and_keep(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("create table t(x)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    out = tmp_path / "backups"
    paths = [backup_sqlite(db, out, keep=3, now=NOW + timedelta(days=i)) for i in range(5)]
    files = sorted(p.name for p in out.glob("bot-*.db"))
    assert files == [p.name for p in paths[-3:]]
    c = sqlite3.connect(paths[-1])
    assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert c.execute("select x from t").fetchone()[0] == 1
    c.close()
