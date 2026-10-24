"""A contact posted into the chat outside the booking/manual-booking contact steps.

``Telegram.WebApp.requestContact`` also posts the user's contact here. The Mini App
normally saves it first (POST /api/me/contact); this is the fallback path. Both call
``users.set_verified_contact`` and are idempotent (docs/WEBAPP.md §2.5).
Included after client_booking and barber_manual (their FSM contact steps win) and
before fallback.
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import views
from app.bot.i18n import t
from app.db.uow import write_tx
from app.services import users
from app.services.errors import ServiceError

router = Router(name="webapp_contact")
router.message.filter(F.chat.type == "private")


@router.message(F.contact)
async def on_any_contact(
    message: Message, sm: async_sessionmaker[AsyncSession], user: Any, lang: str
) -> None:
    contact = message.contact
    assert contact is not None
    if message.from_user is None or contact.user_id != message.from_user.id:
        await message.answer(t(lang, "contact.only_own"))
        return
    try:
        async with write_tx(sm) as s:
            before = await users.get_user(s, user.id)
            res = await users.set_verified_contact(
                s, user_id=user.id, raw_phone=contact.phone_number
            )
    except ServiceError as e:
        await message.answer(views.err_text(lang, e))
        return
    if before.phone_verified and before.phone == res.user.phone:
        return  # the Mini App already saved this exact phone [J-C3]
    await message.answer(t(lang, "contact.saved_webapp"))
