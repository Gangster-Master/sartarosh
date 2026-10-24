"""Default chat menu button: the Mini App when WEBAPP_URL is usable (docs/WEBAPP.md §2.3)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

log = logging.getLogger(__name__)

MENU_TEXT = "Ilova"


async def setup_menu_button(bot: Bot, url: str | None) -> None:
    """Set for every private chat (no chat_id). Without a URL, undo only OUR previous
    button; a button configured by hand in BotFather is left alone. Never blocks startup."""
    try:
        if url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text=MENU_TEXT, web_app=WebAppInfo(url=url))
            )
        else:
            cur = await bot.get_chat_menu_button()
            if isinstance(cur, MenuButtonWebApp) and cur.text == MENU_TEXT:
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as e:
        log.warning("set_chat_menu_button failed: %s", e)
