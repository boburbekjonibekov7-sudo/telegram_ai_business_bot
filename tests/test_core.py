from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_providers import AIService, ManusProvider
from memory_store import MemoryStore
from pause_store import UpstashPauseStore
from postgres_store import PostgresStore
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

    def test_owner_pause_expires_after_thirty_minutes(self) -> None:
        store = MemoryStore()
        store.mark_owner_activity("business:bc:1", time.time() - 60)
        self.assertGreater(store.owner_pause_remaining("business:bc:1"), 1700)
        store.mark_owner_activity("business:bc:1", time.time() - 1801)
        self.assertEqual(store.owner_pause_remaining("business:bc:1"), 0)


class PostgresSelectionTests(unittest.TestCase):
    def test_database_url_builds_postgres_store(self) -> None:
        previous = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "postgresql://example"
            store = PostgresStore.from_env(12)
            self.assertIsInstance(store, PostgresStore)
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous


class PauseStoreTests(unittest.TestCase):
    def test_upstash_pause_uses_ttl_and_calculates_remaining(self) -> None:
        store = UpstashPauseStore("https://redis.example", "token")
        calls = []

        def fake_request(command, key, value=None, query=None):
            calls.append((command, key, value, query))
            return str(time.time() - 60) if command == "get" else "OK"

        store._request = fake_request  # type: ignore[method-assign]
        store.mark_owner_activity("business:bc:1", time.time())
        self.assertEqual(calls[0][0], "set")
        self.assertEqual(calls[0][3], "EX=1800")
        self.assertGreater(store.owner_pause_remaining("business:bc:1"), 1700)


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
        self.assertEqual(bot.telegram.sent[-1]["text"], "Siz admin emassiz.")


class AdminPanelAndApkTests(unittest.TestCase):
    class FakeTelegram:
        def __init__(self):
            self.sent = []
            self.deleted = []
            self.callback_answers = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {"message_id": 10}

        async def delete_business_messages(self, business_connection_id, message_ids):
            self.deleted.append((business_connection_id, message_ids))
            return True

        async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
            self.callback_answers.append((callback_query_id, text, show_alert))
            return True

        async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            self.sent.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
            return True

    class FakeAI:
        async def answer(self, history):
            return "AI javob", "fake"

    def _bot(self):
        settings = SimpleNamespace(
            bot_token="dummy", ai_provider="manus", openai_api_key="", qwen_api_key="", manus_api_key="key",
            openai_base_url="https://api.openai.com/v1", qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_model="gpt-4o-mini", qwen_model="qwen-plus", manus_base_url="https://api.manus.ai",
            manus_agent_profile="manus-1.6-lite", manus_max_wait_seconds=45, system_prompt="default",
            data_dir=Path(tempfile.mkdtemp()), max_history_messages=12, send_error_message=False, admin_user_id=8645314130,
        )
        bot = BusinessAiBot(settings, store=MemoryStore())
        bot.telegram = self.FakeTelegram()
        bot.ai = self.FakeAI()
        bot.connections["bc-1"] = {"id": "bc-1", "is_enabled": True, "rights": {"can_reply": True}}
        return bot

    def test_admin_panel_is_only_available_to_admin(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 9}, "from": {"id": 123}, "text": "/admin"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "Siz admin emassiz.")
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/admin"}}))
        self.assertIn("Admin panel", bot.telegram.sent[-1]["text"])
        self.assertTrue(bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"])

    def test_non_admin_callback_gets_access_denied(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"callback_query": {"id": "cb-1", "from": {"id": 123}, "data": "admin:stats", "message": {"chat": {"id": 9}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.callback_answers[-1], ("cb-1", "Siz admin emassiz.", True))

    def test_apk_business_message_is_deleted(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"business_message": {"message_id": 22, "business_connection_id": "bc-1", "chat": {"id": 777}, "from": {"id": 555}, "document": {"file_name": "setup.apk", "mime_type": "application/vnd.android.package-archive"}}}))
        self.assertEqual(bot.telegram.deleted, [("bc-1", [22])])
        self.assertEqual(bot.telegram.sent, [])


class ManualPauseFlowTests(unittest.TestCase):
    class FakeTelegram:
        def __init__(self):
            self.sent = []

        async def send_typing(self, chat_id, business_connection_id=None):
            return None

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {"message_id": 10}

    class FakeAI:
        def __init__(self):
            self.calls = 0

        async def answer(self, history):
            self.calls += 1
            return "AI javob", "fake"

    def _bot(self):
        settings = SimpleNamespace(
            bot_token="dummy", ai_provider="manus", openai_api_key="", qwen_api_key="", manus_api_key="key",
            openai_base_url="https://api.openai.com/v1", qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_model="gpt-4o-mini", qwen_model="qwen-plus", manus_base_url="https://api.manus.ai",
            manus_agent_profile="manus-1.6-lite", manus_max_wait_seconds=45, system_prompt="default",
            data_dir=Path(tempfile.mkdtemp()), max_history_messages=12, send_error_message=False, admin_user_id=8645314130,
        )
        bot = BusinessAiBot(settings, store=MemoryStore())
        bot.telegram = self.FakeTelegram()
        bot.ai = self.FakeAI()
        bot.connections["bc-1"] = {
            "id": "bc-1", "user": {"id": 8645314130}, "is_enabled": True,
            "rights": {"can_reply": True},
        }
        return bot

    @staticmethod
    def _update(sender_id, text, sender_business_bot=None):
        message = {
            "message_id": 1, "business_connection_id": "bc-1", "chat": {"id": 777},
            "from": {"id": sender_id}, "text": text,
        }
        if sender_business_bot is not None:
            message["sender_business_bot"] = sender_business_bot
        return {"business_message": message}

    def test_owner_message_pauses_customer_and_expiry_allows_ai(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update(self._update(8645314130, "Men keyinroq yozaman")))
        self.assertGreater(bot.store.owner_pause_remaining("business:bc-1:777"), 0)

        asyncio.run(bot.process_update(self._update(555, "Hali bormisiz?")))
        self.assertEqual(bot.ai.calls, 0)
        self.assertEqual(bot.telegram.sent, [])

        bot.store.mark_owner_activity("business:bc-1:777", time.time() - 1801)
        asyncio.run(bot.process_update(self._update(555, "Endi javob bering")))
        self.assertEqual(bot.ai.calls, 1)
        self.assertEqual(bot.telegram.sent[-1]["text"], "AI javob")

    def test_bot_generated_business_message_does_not_start_pause(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update(self._update(8645314130, "Bot yuborgan", {"id": 999})))
        self.assertEqual(bot.store.owner_pause_remaining("business:bc-1:777"), 0)
        self.assertEqual(bot.ai.calls, 0)


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
