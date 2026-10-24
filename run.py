#!/usr/bin/env python3
"""Convenience launcher: ``python run.py`` (same as ``python -m app.bot.main``).

Pass ``--migrate`` to apply database migrations before starting.
"""

import sys

if __name__ == "__main__":
    if "--migrate" in sys.argv[1:]:
        from app.db.migrate import upgrade_head

        upgrade_head()
    from app.bot.main import main

    main()
