"""Audit log writer (ARCHITECTURE.md §4.14)."""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.services import clock


async def write_audit(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    action: str,
    entity: str,
    entity_id: int | None,
    payload: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action[:64],
            entity=entity[:32],
            entity_id=entity_id,
            payload=json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
            created_at=clock.now_utc(),
        )
    )
    await session.flush()
