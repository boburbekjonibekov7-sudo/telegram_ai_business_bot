from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict
from typing import Any

from ai_providers import AIService, ProviderError
from config import Settings
from storage import JsonStore
from telegram_api import TelegramApiError, TelegramBotApi


LOGGER = logging.getLogger("telegram_ai_business_bot")


class BusinessAiBot:
    def __init__(self, settings: Settings, store: Any | None = None):
        self.settings = settings
        self.telegram = TelegramBotApi(settings.bot_token)
        self.ai = AIService(settings)
        self.store = store or JsonStore(settings.data_dir, settings.max_history_messages)
        self.connections: dict[str, dict[str, Any]] = {}
        self.chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.stop_event = asyncio.Event()

    async def startup_check(self) -> None:
        me = await self.telegram.get_me()
        username = me.get("username", "unknown")
        can_connect = me.get("can_connect_to_business")
        if can_connect is False:
            LOGGER.warning(
                "@%s Business Mode yoqilmagan. BotFather’da bot uchun Business Mode’ni yoqing.",
                username,
            )
        else:
            LOGGER.info(
                "Bot @%s ishga tayyor; can_connect_to_business=%s, provider=%s",
                username,
                can_connect,
                self.settings.ai_provider,
            )

    async def run(self) -> None:
        await self.startup_check()
        offset: int | None = None
        allowed_updates = [
            "message",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ]
        delay = 1
        while not self.stop_event.is_set():
            try:
                updates = await self.telegram.get_updates(offset, timeout=30, allowed_updates=allowed_updates)
                delay = 1
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    try:
                        await self.process_update(update)
                    except Exception:
                        LOGGER.exception("Update qayta ishlanmadi: update_id=%s", update_id)
            except TelegramApiError as exc:
                LOGGER.error("Telegram polling xatosi: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            except (OSError, asyncio.TimeoutError) as exc:
                LOGGER.error("Polling tarmoq xatosi: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def process_update(self, update: dict[str, Any]) -> None:
        if "business_connection" in update:
            self._cache_connection(update["business_connection"])
            return
        if "business_message" in update:
            await self.handle_message(update["business_message"], is_business=True)
            return
        if "edited_business_message" in update:
            # Editing an incoming message should not create a second AI answer.
            return
        if "deleted_business_messages" in update:
            return
        if "message" in update:
            await self.handle_message(update["message"], is_business=False)

    def _cache_connection(self, connection: dict[str, Any]) -> None:
        connection_id = connection.get("id")
        if not connection_id:
            return
        self.connections[str(connection_id)] = connection
        if connection.get("is_enabled") is False:
            LOGGER.info("Business ulanish o‘chirildi: %s", connection_id)
        else:
            rights = connection.get("rights") or {}
            LOGGER.info(
                "Business ulanish faol: %s, can_reply=%s",
                connection_id,
                rights.get("can_reply", False),
            )

    async def _get_connection(self, connection_id: str) -> dict[str, Any] | None:
        cached = self.connections.get(connection_id)
        if cached is not None:
            return cached
        try:
            connection = await self.telegram.call(
                "getBusinessConnection",
                {"business_connection_id": connection_id},
            )
        except TelegramApiError as exc:
            LOGGER.warning("Business ulanishini olishda xato: %s", exc)
            return None
        if isinstance(connection, dict):
            self._cache_connection(connection)
            return connection
        return None

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        text = message.get("text") or message.get("caption") or ""
        return str(text).strip()

    @staticmethod
    def _chat_id(message: dict[str, Any]) -> int | None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        return chat_id if isinstance(chat_id, int) else None

    async def handle_message(self, message: dict[str, Any], is_business: bool) -> None:
        # Messages sent by this bot on behalf of the account must not trigger a loop.
        if message.get("sender_business_bot"):
            return
        text = self._message_text(message)
        chat_id = self._chat_id(message)
        if not text or chat_id is None:
            return

        business_connection_id = message.get("business_connection_id") if is_business else None
        if is_business:
            if not isinstance(business_connection_id, str) or not business_connection_id:
                LOGGER.warning("Business message’da connection_id yo‘q; javob yuborilmadi")
                return
            connection = await self._get_connection(business_connection_id)
            if not connection or connection.get("is_enabled") is False:
                LOGGER.warning("Business ulanish faol emas: %s", business_connection_id)
                return
            rights = connection.get("rights") or {}
            if rights.get("can_reply") is not True:
                LOGGER.warning("Business botda can_reply huquqi yo‘q: %s", business_connection_id)
                return

        storage_key = self._storage_key(chat_id, business_connection_id)
        async with self.chat_locks[storage_key]:
            if text.lower() in {"/reset", "/clear"}:
                self.store.clear(storage_key)
                await self._send_chunks(
                    chat_id,
                    "Suhbat tarixi tozalandi.",
                    business_connection_id,
                    message.get("message_id"),
                )
                return

            history = self.store.history(storage_key, self.settings.system_prompt)
            history.append({"role": "user", "content": text})
            try:
                await self.telegram.send_typing(chat_id, business_connection_id)
                answer, provider_name = await self.ai.answer(history)
            except (ProviderError, TelegramApiError) as exc:
                LOGGER.error("AI javobini tayyorlashda xato: %s", exc)
                if self.settings.send_error_message:
                    await self._send_chunks(
                        chat_id,
                        "Hozircha javob tayyorlashda texnik muammo yuz berdi. Keyinroq yana yozib ko‘ring.",
                        business_connection_id,
                        message.get("message_id"),
                    )
                return

            self.store.append(storage_key, "user", text)
            self.store.append(storage_key, "assistant", answer)
            LOGGER.info("Javob yuborildi: chat_id=%s provider=%s", chat_id, provider_name)
            await self._send_chunks(
                chat_id,
                answer,
                business_connection_id,
                message.get("message_id"),
            )

    @staticmethod
    def _storage_key(chat_id: int, business_connection_id: str | None) -> str:
        prefix = f"business:{business_connection_id}" if business_connection_id else "normal"
        return f"{prefix}:{chat_id}"

    async def _send_chunks(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str | None,
        reply_to_message_id: int | None,
    ) -> None:
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)] or ["…"]
        for index, chunk in enumerate(chunks):
            await self.telegram.send_message(
                chat_id=chat_id,
                text=chunk,
                business_connection_id=business_connection_id,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
            )

    def stop(self) -> None:
        self.stop_event.set()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def async_main() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        LOGGER.error("Konfiguratsiya xatosi: %s", exc)
        raise SystemExit(2) from exc
    bot = BusinessAiBot(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            pass
    await bot.run()


if __name__ == "__main__":
    asyncio.run(async_main())
