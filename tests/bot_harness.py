"""Drive the real aiogram stack (middlewares, routers, DbStorage) with a fake Telegram session."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from itertools import count
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

from app.bot.fsm_storage import DbStorage
from app.bot.main import build_dispatcher

BOT_ID = 42
_update_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._msg_ids = count(100)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="test_bot")
        if name.startswith("Send") or name in (
            "EditMessageText",
            "EditMessageMedia",
            "EditMessageCaption",
        ):
            chat_id = getattr(method, "chat_id", None) or 0
            if name.startswith("Edit") and getattr(method, "message_id", None):
                mid = method.message_id  # type: ignore[attr-defined]
            else:
                mid = next(self._msg_ids)
            return Message(
                message_id=mid,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(chat_id), type="private"),
                text=getattr(method, "text", None) or getattr(method, "caption", None),
            ).as_(bot)
        return True

    async def close(self) -> None:  # pragma: no cover
        pass

    async def stream_content(
        self, *a: Any, **k: Any
    ) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        if False:
            yield b""


class Harness:
    def __init__(self, sm: Any) -> None:
        self.session = FakeSession()
        self.bot = Bot(
            token=f"{BOT_ID}:TEST",
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.storage = DbStorage(sm)
        self.dp, self.notifier = build_dispatcher(
            self.bot, sm, self.storage, throttle_per_minute=1000
        )
        self.sm = sm
        from app.bot.middlewares import DebounceMiddleware

        self.debounce = next(
            m for m in self.dp.update.outer_middleware if isinstance(m, DebounceMiddleware)
        )

    # ---------------------------------------------------------------- sending updates
    def _user(self, tg_id: int, lang: str | None = "uz", first_name: str = "Test") -> User:
        return User(id=tg_id, is_bot=False, first_name=first_name, language_code=lang)

    async def text(
        self, tg_id: int, text: str, *, lang: str | None = "uz", first_name: str = "Test"
    ) -> None:
        u = self._user(tg_id, lang, first_name)
        msg = Message(
            message_id=next(self.session._msg_ids),
            date=datetime.now(timezone.utc),
            chat=Chat(id=tg_id, type="private"),
            from_user=u,
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_update_ids), message=msg))

    async def contact(self, tg_id: int, phone: str, *, owner: int | None = None) -> None:
        u = self._user(tg_id)
        msg = Message(
            message_id=next(self.session._msg_ids),
            date=datetime.now(timezone.utc),
            chat=Chat(id=tg_id, type="private"),
            from_user=u,
            contact=Contact(
                phone_number=phone, first_name="X", user_id=owner if owner is not None else tg_id
            ),
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_update_ids), message=msg))

    async def tap(
        self,
        tg_id: int,
        data: str,
        *,
        message_id: int | None = None,
        first_name: str = "Test",
        fast: bool = False,
        reply_markup: Any = None,
    ) -> None:
        if not fast:  # a human doesn't tap the same button twice within a second
            self.debounce._seen.clear()
        u = self._user(tg_id, first_name=first_name)
        mid = message_id if message_id is not None else await self.fsm_msg_id(tg_id)
        msg = Message(
            message_id=mid or 1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=tg_id, type="private"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
            text="…",
            reply_markup=reply_markup,
        )
        cq = CallbackQuery(
            id=str(next(_update_ids)), from_user=u, chat_instance="ci", message=msg, data=data
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_update_ids), callback_query=cq))

    # ---------------------------------------------------------------- inspecting output
    def sent_to(self, chat_id: int) -> list[Any]:
        return [
            c
            for c in self.session.calls
            if getattr(c, "chat_id", None) == chat_id and hasattr(c, "text")
        ]

    def texts_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.sent_to(chat_id) if c.text]

    def key(self, tg_id: int) -> Any:
        from aiogram.fsm.storage.base import StorageKey

        return StorageKey(bot_id=BOT_ID, chat_id=tg_id, user_id=tg_id)

    async def fsm_msg_id(self, tg_id: int) -> int | None:
        return (await self.storage.get_data(self.key(tg_id))).get("msg_id")

    async def fsm_state(self, tg_id: int) -> str | None:
        return await self.storage.get_state(self.key(tg_id))

    def alerts(self) -> list[str]:
        return [
            c.text or ""
            for c in self.session.calls
            if type(c).__name__ == "AnswerCallbackQuery" and c.show_alert
        ]

    def markups(self, chat_id: int) -> list[Any]:
        return [
            getattr(c, "reply_markup", None)
            for c in self.session.calls
            if getattr(c, "chat_id", None) == chat_id
            and getattr(c, "reply_markup", None) is not None
        ]

    def last_callbacks(self, chat_id: int) -> list[str]:
        """callback_data of the last inline keyboard sent/edited to the chat."""
        from aiogram.types import InlineKeyboardMarkup

        for c in reversed(self.session.calls):
            mk = getattr(c, "reply_markup", None)
            cid = getattr(c, "chat_id", None)
            if isinstance(mk, InlineKeyboardMarkup) and (cid == chat_id or cid is None):
                return [
                    b.callback_data for row in mk.inline_keyboard for b in row if b.callback_data
                ]
        return []
