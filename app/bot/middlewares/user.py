"""User/roles/lang injection, consent gate and blocked-user drop (ARCHITECTURE.md §5.3).

1. ``read_tx``: ``users.load_context`` (no write lock for the common case [A2]).
2. Unknown user, changed profile, or ``/start <payload>`` → ``ensure_user`` in ``write_tx``.
3. Barber marked unreachable sends anything → reachable again [B4].
4. Inject ``user``, ``roles``, ``lang`` (and ``ctx``).
5. Blocked users get one notice per 10 minutes; the update is dropped.
6. Consent gate: before consent only onboarding updates pass.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.i18n import norm_lang, t
from app.config import CONSENT_VERSION
from app.db.uow import read_tx, write_tx
from app.services import barbers, users

log = logging.getLogger(__name__)

BLOCK_NOTICE_EVERY = 600.0
_CONSENT_FREE_COMMANDS = ("/start", "/whoami", "/delete_me", "/lang")
_CONSENT_FREE_CALLBACKS = ("lg:", "cs:", "dm:", "noop")


def _start_payload(text: str | None) -> str | None:
    if not text or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if parts[0] not in ("/start",) and not parts[0].startswith("/start@"):
        return None
    return parts[1].strip() if len(parts) > 1 else None


def _command(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


class UserMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._block_noticed: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or not isinstance(event, Update):
            return await handler(event, data)
        sm: async_sessionmaker[AsyncSession] = data["sm"]

        text = event.message.text if event.message is not None else None
        payload = _start_payload(text)

        async with read_tx(sm) as s:
            ctx = await users.load_context(s, tg_id=tg_user.id)

        if (
            ctx is None
            or payload
            or users.needs_sync(
                ctx.user, tg_username=tg_user.username, first_name=tg_user.first_name or ""
            )
        ):
            async with write_tx(sm) as s:
                ctx = await users.ensure_user(
                    s,
                    tg_id=tg_user.id,
                    tg_username=tg_user.username,
                    first_name=tg_user.first_name or "",
                    language_code=tg_user.language_code,
                    start_payload=payload,
                )

        if ctx.roles.is_barber and ctx.barber_reachable is False:
            async with write_tx(sm) as s:
                await barbers.mark_reachable_by_user(s, user_id=ctx.user.id)

        lang = norm_lang(ctx.user.lang)
        data["ctx"] = ctx
        data["user"] = ctx.user
        data["roles"] = ctx.roles
        data["lang"] = lang

        bot: Bot = data["bot"]
        if ctx.user.is_blocked:
            now = time.monotonic()
            last = self._block_noticed.get(tg_user.id)
            if last is None or now - last > BLOCK_NOTICE_EVERY:
                self._block_noticed[tg_user.id] = now
                if len(self._block_noticed) > 10_000:
                    self._block_noticed.clear()
                try:
                    await bot.send_message(tg_user.id, t(lang, "common.blocked"))
                except TelegramAPIError:
                    pass
            if event.callback_query is not None:
                try:
                    await event.callback_query.answer()
                except TelegramAPIError:
                    pass
            return None

        if (ctx.user.consent_version or 0) < CONSENT_VERSION and not self._consent_free(event):
            from app.bot import views  # local import: views imports keyboards that import i18n

            if event.callback_query is not None:
                try:
                    await event.callback_query.answer()
                except TelegramAPIError:
                    pass
            await views.send_onboarding(bot, tg_user.id, ctx.user, lang, tg_user.language_code)
            return None

        return await handler(event, data)

    @staticmethod
    def _consent_free(event: Update) -> bool:
        if event.message is not None:
            return _command(event.message.text) in _CONSENT_FREE_COMMANDS
        if event.callback_query is not None:
            payload = event.callback_query.data or ""
            return payload.startswith(_CONSENT_FREE_CALLBACKS)
        # my_chat_member etc. carry no user intent
        return True
