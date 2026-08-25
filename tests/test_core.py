from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_providers import AIService, ManusProvider
from memory_store import MemoryStore
from storage import JsonStore
from app import BusinessAiBot
from telegram_api import TelegramBotApi


class StorageTests(unittest.TestCase):
    def test_history_is_limited_and_resettable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory), max_history_messages=2)
            store.append("business:c:1", "user", "salom")
            store.append("business:c:1", "assistant", "assalom")
            store.append("business:c:1", "user", "yana savol")
            history = store.history("business:c:1", "system")
            self.assertEqual(
                history,
                [
                    {"role": "system", "content": "system"},
                    {"role": "assistant", "content": "assalom"},
                    {"role": "user", "content": "yana savol"},
                ],
            )
            store.clear("business:c:1")
            self.assertEqual(store.history("business:c:1", "system"), [{"role": "system", "content": "system"}])

    def test_role_survives_json_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path)
            store.set_role("Faqat qisqa javob bering")
            reloaded = JsonStore(path)
            self.assertEqual(reloaded.get_role("default"), "Faqat qisqa javob bering")

    def test_memory_store_role_can_reset(self) -> None:
        store = MemoryStore()
        store.set_role("Rasmiy uslub")
        self.assertEqual(store.get_role("default"), "Rasmiy uslub")
        store.clear_role()
        self.assertEqual(store.get_role("default"), "default")


class ProviderSelectionTests(unittest.TestCase):
    def _settings(self, provider: str) -> SimpleNamespace:
        return SimpleNamespace(
            ai_provider=provider,
            openai_api_key="openai-key",
            qwen_api_key="qwen-key",
            openai_base_url="https://api.openai.com/v1",
            qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_model="gpt-4o-mini",
            qwen_model="qwen-plus",
        )

    def test_auto_prefers_openai_then_qwen(self) -> None:
        service = AIService(self._settings("auto"))
        self.assertEqual(service._provider_order(), ["openai", "qwen"])

    def test_explicit_qwen(self) -> None:
        service = AIService(self._settings("qwen"))
        self.assertEqual(service._provider_order(), ["qwen"])

    def test_manus_prompt_and_output_parsing(self) -> None:
        prompt = ManusProvider._prompt([
            {"role": "system", "content": "Qisqa yoz."},
            {"role": "user", "content": "Salom"},
        ])
        self.assertIn("Ko‘rsatma:", prompt)
        self.assertIn("Foydalanuvchi: Salom", prompt)
        answer = ManusProvider._assistant_text([
            {"type": "status_update", "status_update": {"agent_status": "stopped"}},
            {"type": "assistant_message", "assistant_message": {"content": "Assalom"}},
        ])
        self.assertEqual(answer, "Assalom")


class RoleCommandTests(unittest.TestCase):
    def test_admin_can_set_and_reset_role(self) -> None:
        class FakeTelegram:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return {"message_id": 1}

            async def send_chat_action(self, **kwargs):
                return None

        settings = SimpleNamespace(
            bot_token="dummy",
            ai_provider="openai",
            openai_api_key="openai-key",
            qwen_api_key="",
            openai_base_url="https://api.openai.com/v1",
            qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_model="gpt-4o-mini",
            qwen_model="qwen-plus",
            system_prompt="default prompt",
            data_dir=Path(tempfile.mkdtemp()),
            max_history_messages=12,
            send_error_message=False,
            admin_user_id=123,
        )
        bot = BusinessAiBot(settings, store=MemoryStore())
        bot.telegram = FakeTelegram()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 123}, "from": {"id": 123}, "text": "/rol Qisqa va rasmiy javob ber"}}))
        self.assertEqual(bot.store.get_role("default"), "Qisqa va rasmiy javob ber")
        self.assertIn("Yangi rol saqlandi", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 123}, "from": {"id": 123}, "text": "/rol reset"}}))
        self.assertEqual(bot.store.get_role("default"), "default")

    def test_non_admin_cannot_set_role(self) -> None:
        class FakeTelegram:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return {"message_id": 1}

        settings = SimpleNamespace(
            bot_token="dummy", ai_provider="openai", openai_api_key="openai-key", qwen_api_key="",
            openai_base_url="https://api.openai.com/v1", qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_model="gpt-4o-mini", qwen_model="qwen-plus", system_prompt="default", data_dir=Path(tempfile.mkdtemp()),
            max_history_messages=12, send_error_message=False, admin_user_id=123,
        )
        bot = BusinessAiBot(settings, store=MemoryStore())
        bot.telegram = FakeTelegram()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 999}, "from": {"id": 999}, "text": "/rol yomon"}}))
        self.assertEqual(bot.store.get_role("default"), "default")
        self.assertIn("faqat akkaunt egasi", bot.telegram.sent[-1]["text"])


class TelegramPayloadTests(unittest.TestCase):
    def test_business_send_payload_contains_connection_id(self) -> None:
        bot = TelegramBotApi("dummy")
        captured = {}

        def fake_call(method, payload):
            captured["method"] = method
            captured["payload"] = payload
            return {"message_id": 1}

        bot._call_sync = fake_call  # type: ignore[method-assign]
        result = bot._call_sync("sendMessage", {"chat_id": 1, "text": "ok", "business_connection_id": "bc"})
        self.assertEqual(result["message_id"], 1)
        self.assertEqual(captured["payload"]["business_connection_id"], "bc")


if __name__ == "__main__":
    unittest.main()
