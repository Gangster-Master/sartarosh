"""Key/value persistence for the bot FSM storage (ARCHITECTURE.md §4.14).

Used only by app/bot/fsm_storage.py; each call runs in its own short tx.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FsmState
from app.services import clock

KEY_MAX = 128


def _key(key: str) -> str:
    if len(key) > KEY_MAX:
        raise ValueError(f"FSM key longer than {KEY_MAX} chars")
    return key


async def get_record(session: AsyncSession, key: str) -> tuple[str | None, dict]:
    row = await session.get(FsmState, _key(key))
    if row is None:
        return None, {}
    try:
        data = json.loads(row.data or "{}")
    except ValueError:
        data = {}
    return row.state, data if isinstance(data, dict) else {}


async def _row(session: AsyncSession, key: str) -> FsmState:
    row = await session.get(FsmState, _key(key))
    if row is None:
        row = FsmState(key=key, state=None, data="{}", updated_at=clock.now_utc())
        session.add(row)
    return row


async def set_state(session: AsyncSession, key: str, state: str | None) -> None:
    row = await _row(session, key)
    row.state = state[:128] if state else None
    row.updated_at = clock.now_utc()
    await session.flush()


async def set_data(session: AsyncSession, key: str, data: dict) -> None:
    row = await _row(session, key)
    row.data = json.dumps(data or {}, ensure_ascii=False, default=str)
    row.updated_at = clock.now_utc()
    await session.flush()


async def purge_older_than(
    session: AsyncSession, *, days: int = 7, now: datetime | None = None
) -> int:
    now = clock.ensure_aware(now) if now else clock.now_utc()
    res = await session.execute(
        delete(FsmState).where(FsmState.updated_at < now - timedelta(days=days))
    )
    return int(res.rowcount or 0)
