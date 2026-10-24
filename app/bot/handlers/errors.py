"""Global error handler [E4]: log, stop the callback spinner, alert admins (rate-limited)."""

from __future__ import annotations

import logging
import time

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from app.bot.i18n import norm_lang, t
from app.bot.notifier import Notifier

log = logging.getLogger(__name__)

ALERT_EVERY = 300.0
_last_alert: dict[str, float] = {}


async def on_error(
    event: ErrorEvent, notifier: Notifier | None = None, lang: str | None = None
) -> bool:
    exc = event.exception
    update = event.update
    log.error("unhandled error in update %s: %r", update.update_id, exc, exc_info=exc)
    if update.callback_query is not None:
        cb_lang = norm_lang(lang or (update.callback_query.from_user.language_code or "")[:2])
        try:
            await update.callback_query.answer(t(cb_lang, "err.error"), show_alert=True)
        except TelegramAPIError:
            pass
    elif update.message is not None:
        code = update.message.from_user.language_code if update.message.from_user else None
        msg_lang = norm_lang(lang or (code or "")[:2])
        try:
            await update.message.answer(t(msg_lang, "err.error"))
        except TelegramAPIError:
            pass
    name = type(exc).__name__
    now = time.monotonic()
    if notifier is not None and now - _last_alert.get(name, 0.0) > ALERT_EVERY:
        _last_alert[name] = now
        await notifier.alert_admins(
            "admin.crash", error=name, detail=str(exc)[:300].replace("<", "‹").replace(">", "›")
        )
    return True
