"""[P2] Rating 1–5 after a visit [#54]; low ratings go to the admins privately."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import screen, views
from app.bot.callbacks import RateCb
from app.bot.i18n import t
from app.bot.notifier import Notifier
from app.db.uow import write_tx
from app.services import reviews
from app.services.errors import ServiceError

router = Router(name="client_feedback")
router.message.filter(F.chat.type == "private")


@router.callback_query(RateCb.filter(), StateFilter("*"))
async def on_rate(
    call: CallbackQuery,
    callback_data: RateCb,
    sm: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    user: Any,
    lang: str,
) -> None:
    if not 1 <= callback_data.v <= 5:
        await views.stale(call, lang)
        return
    try:
        async with write_tx(sm) as s:
            r = await reviews.submit_rating(
                s, booking_id=callback_data.id, client_user_id=user.id, rating=callback_data.v
            )
    except ServiceError as e:
        await views.alert(call, views.err_text(lang, e))
        return
    key = "feedback.thanks_low" if r.is_low else "feedback.thanks"
    await screen.render(call, None, t(lang, key, stars="⭐" * r.rating))
    if r.is_low and r.created:
        await notifier.low_rating(r)
