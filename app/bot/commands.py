"""Bot command lists per language and per role [#88]."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.bot.i18n import LANGS, t

log = logging.getLogger(__name__)

CLIENT_CMDS = ("start", "menu", "my", "lang", "help", "whoami", "cancel", "delete_me")
BARBER_CMDS = ("start", "menu", "today", "my", "lang", "help", "whoami", "cancel")
ADMIN_CMDS = ("start", "menu", "today", "lang", "help", "whoami", "cancel")


def _cmds(names: tuple[str, ...], lang: str) -> list[BotCommand]:
    return [BotCommand(command=n, description=t(lang, f"cmd.{n}")) for n in names]


async def set_default_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(_cmds(CLIENT_CMDS, "uz"), scope=BotCommandScopeDefault())
        for lang in LANGS:
            await bot.set_my_commands(
                _cmds(CLIENT_CMDS, lang), scope=BotCommandScopeDefault(), language_code=lang
            )
    except TelegramAPIError as e:
        log.warning("set_my_commands failed: %s", e)


async def set_user_commands(bot: Bot, tg_id: int, lang: str, roles: Any) -> None:
    """Per-chat command list for staff; clients get the default list back."""
    try:
        scope = BotCommandScopeChat(chat_id=tg_id)
        if roles is not None and roles.is_admin:
            names = ADMIN_CMDS
        elif roles is not None and roles.is_barber:
            names = BARBER_CMDS
        else:
            await bot.delete_my_commands(scope=scope)
            return
        await bot.set_my_commands(_cmds(names, lang), scope=scope)
    except TelegramAPIError as e:
        log.debug("set_user_commands(%s) failed: %s", tg_id, e)
