"""Demo seed: ``python -m app.db.seed --demo`` (ARCHITECTURE.md §3.6).

Refuses without ``--demo`` and refuses if any barber exists unless
``--force-demo`` is passed [G3]. Barbers are created unlinked (hidden from
clients) and their invite payloads are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from datetime import time, timedelta

from sqlalchemy import func, select

from app.db.models import Barber, Service, Setting, WorkingHours
from app.db.session import create_engine, create_sessionmaker
from app.db.uow import write_tx
from app.services import clock

DEMO_SERVICES = [("Soch olish", 30, 60_000), ("Soqol", 15, 30_000), ("Soch + soqol", 60, 80_000)]
DEMO_BARBERS = ["Akmal", "Bobur", "Sardor"]
DEMO_SHOP = {
    "shop_name": "Demo Sartaroshxona",
    "address": "Toshkent, Amir Temur ko'chasi, 1",
    "contact_phone": "+998901234567",
}


async def seed_demo(url: str | None = None, *, force: bool = False) -> list[tuple[str, str]]:
    engine = create_engine(url)
    sm = create_sessionmaker(engine)
    out: list[tuple[str, str]] = []
    try:
        async with write_tx(sm) as s:
            count = (await s.execute(select(func.count()).select_from(Barber))).scalar_one()
            if count and not force:
                raise SystemExit("barbers already exist; pass --force-demo to seed anyway")
            now = clock.now_utc()
            for i, (name, dur, price) in enumerate(DEMO_SERVICES, start=1):
                s.add(
                    Service(
                        name=name,
                        duration_min=dur,
                        price_uzs=price,
                        sort_order=i,
                        created_at=now,
                        updated_at=now,
                    )
                )
            for i, name in enumerate(DEMO_BARBERS, start=1):
                token = secrets.token_urlsafe(12)
                b = Barber(
                    display_name=name,
                    sort_order=i,
                    invite_token=token,
                    invite_expires_at=now + timedelta(hours=24),
                    created_at=now,
                    updated_at=now,
                )
                s.add(b)
                await s.flush()
                for wd in range(6):  # Mon..Sat
                    s.add(
                        WorkingHours(
                            barber_id=b.id,
                            weekday=wd,
                            start_time=time(10),
                            end_time=time(20),
                            break_start=time(13),
                            break_end=time(14),
                        )
                    )
                out.append((name, token))
            for key, value in DEMO_SHOP.items():
                row = await s.get(Setting, key)
                data = json.dumps(value, ensure_ascii=False)
                if row is None:
                    s.add(Setting(key=key, value=data, updated_at=now))
                else:
                    row.value = data
    finally:
        await engine.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.db.seed")
    p.add_argument("--demo", action="store_true", help="create demo data")
    p.add_argument("--force-demo", action="store_true", help="seed even if barbers exist")
    p.add_argument("--url", default=None, help="DATABASE_URL override")
    args = p.parse_args(argv)
    if not args.demo:
        print("refusing to run without --demo", file=sys.stderr)
        return 2
    invites = asyncio.run(seed_demo(args.url, force=args.force_demo))
    print("Demo data created. Barber invite links (valid 24 h):")
    for name, token in invites:
        print(f"  {name}: https://t.me/<bot_username>?start=inv_{token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
