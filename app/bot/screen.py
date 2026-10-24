"""Edit-in-place rendering (ARCHITECTURE.md §6.7).

``render`` keeps a flow on one message: a callback edits the message it came from
when the kind matches (text→text, photo→photo); otherwise the old message is
deleted (best effort) and a new one is sent. The resulting message id is stored
in FSM data as ``msg_id`` so stale buttons can be detected.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def clip(text: str, limit: int = TEXT_LIMIT - 96) -> str:
    """Keep a message under Telegram's limit. Cuts at a line break, so HTML tags
    (always opened and closed within one line here) stay balanced."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip() + "\n…"


def _not_modified(e: Exception) -> bool:
    return "message is not modified" in str(e).lower()


async def safe_answer(call: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await call.answer(text, show_alert=alert)
    except TelegramAPIError:
        pass  # query too old / already answered


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        pass


async def strip_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramAPIError:
        pass


async def render(
    target: CallbackQuery | Message,
    state: FSMContext | None,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    *,
    photo: str | None = None,
    new_message: bool = False,
) -> int:
    """Show ``text`` (+ optional photo) with ``kb``; returns the message id."""
    text = clip(text)
    bot: Bot = target.bot  # type: ignore[assignment]
    sent_id: int | None = None
    is_call = isinstance(target, CallbackQuery)

    if is_call:
        chat_id = target.from_user.id
        old = target.message if isinstance(target.message, Message) else None
        if old is not None:
            chat_id = old.chat.id
    else:
        chat_id = target.chat.id
        old = None

    if photo and len(text) > CAPTION_LIMIT:
        photo = None  # caption would not fit: fall back to text

    if is_call and old is not None and not new_message:
        old_is_photo = bool(old.photo)
        try:
            if photo is None and not old_is_photo:
                await old.edit_text(text, reply_markup=kb)
                sent_id = old.message_id
            elif photo is not None and old_is_photo:
                await old.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=kb)
                sent_id = old.message_id
        except TelegramBadRequest as e:
            if _not_modified(e):
                sent_id = old.message_id
            else:
                log.debug("edit failed, sending new message: %s", e)
        except TelegramAPIError as e:
            log.debug("edit failed, sending new message: %s", e)

    if sent_id is None:
        if is_call and old is not None:
            await safe_delete(bot, chat_id, old.message_id)
        elif not is_call and state is not None:
            # a typed answer: take the buttons off the previous screen so they can't be reused
            prev = (await state.get_data()).get("msg_id")
            await strip_keyboard(bot, chat_id, prev)
        if photo:
            msg = await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=kb)
        else:
            msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        sent_id = msg.message_id

    if state is not None:
        await state.update_data(msg_id=sent_id)
    if is_call:
        await safe_answer(target)
    return sent_id


async def send_reply_kb(
    bot: Bot, chat_id: int, text: str, kb: ReplyKeyboardMarkup | ReplyKeyboardRemove
) -> Message | None:
    """Send a short message carrying a reply keyboard (reply keyboards can't be edited in)."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    except TelegramAPIError as e:
        log.debug("send_reply_kb failed: %s", e)
        return None
