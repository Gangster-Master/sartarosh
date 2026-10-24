"""Role filters. They read ``data["roles"]`` injected by UserMiddleware, which is
re-read from the DB on every update [H2]. Services check permissions again."""

from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, roles: Any = None) -> bool:
        return bool(roles is not None and roles.is_admin)


class IsBarber(Filter):
    async def __call__(self, event: TelegramObject, roles: Any = None) -> bool:
        return bool(roles is not None and roles.is_barber and roles.barber_id)


class IsStaff(Filter):
    """Barber or admin (booking decisions, booking cards)."""

    async def __call__(self, event: TelegramObject, roles: Any = None) -> bool:
        return bool(roles is not None and (roles.is_admin or (roles.is_barber and roles.barber_id)))


class IsPrivate(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        if isinstance(event, Message):
            return event.chat.type == "private"
        if isinstance(event, CallbackQuery):
            msg = event.message
            return msg is None or msg.chat.type == "private"
        return True
