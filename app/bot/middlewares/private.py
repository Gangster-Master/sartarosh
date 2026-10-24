"""Drop every update that is not from a private chat; leave groups [E7][#90]."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, TelegramObject, Update

log = logging.getLogger(__name__)


class PrivateOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None = data.get("event_chat")
        if chat is None:
            return None  # inline queries, polls, channel posts…: not used by this bot
        if chat.type == "private":
            user = data.get("event_from_user")
            if user is not None and user.is_bot:
                return None
            return await handler(event, data)
        if isinstance(event, Update) and event.my_chat_member is not None:
            new_status = event.my_chat_member.new_chat_member.status
            if new_status in ("member", "administrator", "restricted"):
                bot: Bot = data["bot"]
                try:
                    await bot.leave_chat(chat.id)
                    log.info("left non-private chat %s", chat.id)
                except TelegramAPIError as e:
                    log.debug("leave_chat failed: %s", e)
        return None
