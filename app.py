from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import Settings
from telegram_api import TelegramBotApi

LOGGER = logging.getLogger("telegram_ai_business_bot")


class BusinessAiBot:
    """Minimal Telegram Business bot skeleti.

    Eski asosiy menyu, buyruqlar, AI, VIP, media downloader va admin oqimlari
    ataylab olib tashlandi. Yangi funksiyalar shu klassga bosqichma-bosqich
    qo‘shiladi.
    """

    def __init__(self, settings: Settings, store: Any | None = None):
        self.settings = settings
        self.telegram = TelegramBotApi(settings.bot_token)
        self.store = store
        self.stop_event = asyncio.Event()

    async def startup_check(self) -> None:
        me = await self.telegram.get_me()
        LOGGER.info(
            "Minimal bot tayyor: @%s, business=%s",
            me.get("username", "unknown"),
            me.get("can_connect_to_business"),
        )

    async def run(self) -> None:
        await self.startup_check()
        offset: int | None = None
        while not self.stop_event.is_set():
            updates = await self.telegram.get_updates(
                offset,
                timeout=30,
                allowed_updates=["message", "business_connection", "business_message"],
            )
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    await self.process_update(update)
                except Exception:
                    LOGGER.exception("Update qayta ishlanmadi: %s", update_id)

    async def process_update(self, update: dict[str, Any]) -> None:
        if "business_connection" in update:
            LOGGER.info("Business connection qabul qilindi")
            return

        message = update.get("message") or update.get("business_message")
        if not isinstance(message, dict):
            return

        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        # Yangi loyiha uchun faqat minimal javob. Hech qanday menyu yoki eski
        # buyruq funksiyalari yo‘q.
        if text in {"/start", "/help"}:
            await self.telegram.send_message(
                chat_id=chat_id,
                text="Bot qayta qurilmoqda. Yangi funksiyalar tez orada qo‘shiladi.",
            )


def main() -> None:
    settings = Settings.from_env()
    bot = BusinessAiBot(settings)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        LOGGER.info("Bot to‘xtatildi")


if __name__ == "__main__":
    main()
