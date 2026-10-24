#!/usr/bin/env python3
"""Convenience launcher: ``python run.py`` (same as ``python -m app.bot.main``).

Pass ``--migrate`` to apply database migrations before starting. The single-instance
lock is taken first, so a second copy started while the bot runs exits (code 3)
without touching the live database.
"""

import sys

if __name__ == "__main__":
    if "--migrate" in sys.argv[1:]:
        from app.bot.main import _take_lock
        from app.db.migrate import upgrade_head

        _take_lock()
        upgrade_head()
    from app.bot.main import main

    main()
