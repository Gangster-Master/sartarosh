"""DB-backed FSM storage (ARCHITECTURE.md §5.2, [E3]).

Wizard state survives a restart. Reads go through a small write-through cache
(single process, so the cache is coherent); every write is persisted at once in
its own short transaction via ``app.services.fsm_store``.
"""

from __future__ import annotations

import copy
import json
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.uow import read_tx, write_tx
from app.services import fsm_store

_CACHE_MAX = 5000


def _key(key: StorageKey) -> str:
    parts = [str(key.bot_id), str(key.chat_id), str(key.user_id)]
    if key.thread_id:
        parts.append(f"t{key.thread_id}")
    if key.business_connection_id:
        parts.append(f"b{key.business_connection_id}")
    if key.destiny and key.destiny != "default":
        parts.append(key.destiny)
    return ":".join(parts)[:128]


def _state_str(state: StateType) -> str | None:
    if state is None:
        return None
    if isinstance(state, State):
        return state.state
    return str(state)


def _jsonable(data: Mapping[str, Any]) -> dict[str, Any]:
    # round-trip through JSON so the cache holds exactly what the DB would return
    return json.loads(json.dumps(dict(data), default=str))


class DbStorage(BaseStorage):
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm
        self._cache: OrderedDict[str, tuple[str | None, dict[str, Any]]] = OrderedDict()

    def _remember(self, k: str, state: str | None, data: dict[str, Any]) -> None:
        self._cache[k] = (state, data)
        self._cache.move_to_end(k)
        while len(self._cache) > _CACHE_MAX:
            self._cache.popitem(last=False)

    async def _load(self, k: str) -> tuple[str | None, dict[str, Any]]:
        hit = self._cache.get(k)
        if hit is not None:
            self._cache.move_to_end(k)
            return hit
        async with read_tx(self._sm) as s:
            state, data = await fsm_store.get_record(s, k)
        rec = (state, dict(data or {}))
        self._remember(k, *rec)
        return rec

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        k = _key(key)
        value = _state_str(state)
        async with write_tx(self._sm) as s:
            await fsm_store.set_state(s, k, value)
        hit = self._cache.get(k)
        data = hit[1] if hit is not None else (await self._load(k))[1]
        self._remember(k, value, data)

    async def get_state(self, key: StorageKey) -> str | None:
        state, _ = await self._load(_key(key))
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        k = _key(key)
        clean = _jsonable(data)
        async with write_tx(self._sm) as s:
            await fsm_store.set_data(s, k, clean)
        state, _ = await self._load(k)
        self._remember(k, state, clean)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        _, data = await self._load(_key(key))
        return copy.deepcopy(data)

    async def close(self) -> None:
        self._cache.clear()
