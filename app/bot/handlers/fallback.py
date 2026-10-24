"""Last router: an unknown message re-shows the hub (or nudges inside a flow);
an unknown callback answers STALE."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import views
from app.bot.i18n import t
from app.bot.keyboards.common import menu_for

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


@router.message(StateFilter("*"))
async def on_any_message(
    message: Message,
    state: FSMContext,
    sm: async_sessionmaker[AsyncSession],
    user: Any,
    roles: Any,
    lang: str,
) -> None:
    if await state.get_state() is not None:
        await message.answer(t(lang, "common.use_buttons"))
        return
    if message.contact is not None:
        await message.answer(t(lang, "contact.not_needed"), reply_markup=menu_for(roles, lang))
        return
    await views.send_home(message, state, sm, user, roles, lang, prefix=t(lang, "common.unknown"))


@router.callback_query(StateFilter("*"))
async def on_any_callback(call: CallbackQuery, lang: str) -> None:
    await views.stale(call, lang)
