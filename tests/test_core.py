from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import BusinessAiBot
from config import Settings


class MinimalBotTests(unittest.TestCase):
    def test_settings_requires_bot_token(self) -> None:
        previous = os.environ.pop("BOT_TOKEN", None)
        try:
            with self.assertRaises(ValueError):
                Settings.from_env()
        finally:
            if previous is not None:
                os.environ["BOT_TOKEN"] = previous

    def test_start_and_help_have_no_menu(self) -> None:
        class FakeTelegram:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)

        bot = BusinessAiBot(SimpleNamespace(bot_token="dummy"))
        bot.telegram = FakeTelegram()
        asyncio.run(bot.process_update({
            "message": {"chat": {"id": 42}, "text": "/start"}
        }))
        self.assertEqual(len(bot.telegram.sent), 1)
        self.assertNotIn("inline_keyboard", str(bot.telegram.sent[0]))

    def test_old_menu_commands_are_ignored(self) -> None:
        class FakeTelegram:
            async def send_message(self, **kwargs):
                raise AssertionError("old command should not send a response")

        bot = BusinessAiBot(SimpleNamespace(bot_token="dummy"))
        bot.telegram = FakeTelegram()
        asyncio.run(bot.process_update({
            "message": {"chat": {"id": 42}, "text": ".settings"}
        }))


if __name__ == "__main__":
    unittest.main()
