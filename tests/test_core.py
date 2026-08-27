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

    def test_user_settings_and_business_profiles_are_isolated(self) -> None:
        store = MemoryStore()
        store.set_user_role(100, "User 100 roli")
        store.set_user_role(200, "User 200 roli")
        store.set_user_manual_pause_enabled(100, False)
        self.assertEqual(store.get_user_role(100), "User 100 roli")
        self.assertEqual(store.get_user_role(200), "User 200 roli")
        self.assertFalse(store.user_manual_pause_enabled(100))
        self.assertTrue(store.user_manual_pause_enabled(200))
        store.upsert_business_profile("bc-100", 100)
        store.set_business_role("bc-100", "Business 100 roli")
        self.assertEqual(store.get_business_role("bc-100"), "Business 100 roli")
        self.assertEqual(store.get_business_role("bc-200"), "")


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

    def test_postgres_store_accepts_normal_and_business_keys(self) -> None:
        self.assertEqual(PostgresStore._parts("normal:123"), ("__normal__", 123))
        self.assertEqual(PostgresStore._parts("business:connection:456"), ("connection", 456))


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
            admin_user_id=8645314130,
        )
        bot = BusinessAiBot(settings, store=MemoryStore())
        bot.telegram = FakeTelegram()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/rol Qisqa va rasmiy javob ber"}}))
        self.assertEqual(bot.store.get_role("default"), "Qisqa va rasmiy javob ber")
        self.assertIn("Yangi rol saqlandi", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/rol reset"}}))
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
            self.invoice_links = []
            self.pre_checkout_answers = []
            self.forwarded = []
            self.photos = []
            self.videos = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {"message_id": 10}

        async def send_photo(self, chat_id, photo, caption=None, business_connection_id=None, reply_markup=None):
            item = {"chat_id": chat_id, "photo": photo, "caption": caption, "reply_markup": reply_markup}
            self.photos.append(item)
            self.sent.append(item)
            return {"message_id": 11}

        async def send_video(self, chat_id, video, caption=None, business_connection_id=None, reply_markup=None):
            item = {"chat_id": chat_id, "video": video, "caption": caption, "reply_markup": reply_markup}
            self.videos.append(item)
            self.sent.append(item)
            return {"message_id": 12}

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))
            return True

        async def forward_message(self, chat_id, from_chat_id, message_id):
            self.forwarded.append((chat_id, from_chat_id, message_id))
            return {"message_id": 11}

        async def send_typing(self, chat_id, business_connection_id=None):
            return None

        async def create_invoice_link(self, title, description, payload, amount, subscription_period):
            self.invoice_links.append({"title": title, "description": description, "payload": payload, "amount": amount, "subscription_period": subscription_period})
            return "https://t.me/invoice/test"

        async def answer_pre_checkout_query(self, query_id, ok, error_message=None):
            self.pre_checkout_answers.append((query_id, ok, error_message))
            return True

        async def delete_business_messages(self, business_connection_id, message_ids):
            self.deleted.append((business_connection_id, message_ids))
            return True

        async def get_chat(self, chat_id):
            return {"id": -100123, "title": "Test kanal", "username": "test_channel"}

        async def get_chat_member(self, chat_id, user_id):
            return {"status": "left"}

        async def get_chat_join_requests(self, chat_id, user_id=None, invite_link=None, limit=1):
            return []

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

    def test_pause_toggle_is_persistent_in_store(self) -> None:
        bot = self._bot()
        self.assertTrue(bot._manual_pause_enabled())
        asyncio.run(bot.process_update({"callback_query": {"id": "cb-pause", "from": {"id": 8645314130}, "data": "admin:pause:toggle", "message": {"chat": {"id": 8645314130}, "message_id": 20}}}))
        self.assertFalse(bot._manual_pause_enabled())
        self.assertIn("O‘CHIRILGAN", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"callback_query": {"id": "cb-pause-2", "from": {"id": 8645314130}, "data": "admin:pause:toggle", "message": {"chat": {"id": 8645314130}, "message_id": 20}}}))
        self.assertTrue(bot._manual_pause_enabled())

    def test_only_mangenkyo_promo_grants_once(self) -> None:
        bot = self._bot()
        user = {"id": 1234, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1234}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1234}, "from": user, "text": "Mangekyo Sharingan"}}))
        self.assertFalse(bot.store.has_premium(1234))
        self.assertEqual(bot.telegram.sent[-1]["text"], "So‘rov bajarilmadi.")
        asyncio.run(bot.process_update({"message": {"message_id": 3, "chat": {"id": 1234}, "from": user, "text": "Mangenkyo Sharingan"}}))
        self.assertIn("Sharingan faollashdi!\nEndi siz botdan 1 oy bepul foydalanasiz!!!\n/start /start /start", bot.telegram.sent[-1]["text"])
        self.assertTrue(bot.store.has_premium(1234))
        asyncio.run(bot.process_update({"message": {"message_id": 4, "chat": {"id": 1234}, "from": user, "text": "Mangenkyo Sharingan"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "So‘rov bajarilmadi.")

    def test_mangenkyo_typo_variant_grants_premium_once(self) -> None:
        bot = self._bot()
        user = {"id": 1238, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1238}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1238}, "from": user, "text": "Mangenkyo Sharingan"}}))
        self.assertTrue(bot.store.has_premium(1238))
        self.assertIn("Sharingan faollashdi!", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"message": {"message_id": 3, "chat": {"id": 1238}, "from": user, "text": "Mangenkyo Sharingan"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "So‘rov bajarilmadi.")

    def test_premium_user_gets_admin_without_statistics(self) -> None:
        bot = self._bot()
        user = {"id": 1239, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1239}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1239}, "from": user, "text": "Mangenkyo Sharingan"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 3, "chat": {"id": 1239}, "from": user, "text": "/admin"}}))
        panel = bot.telegram.sent[-1]
        self.assertEqual(panel["text"], "👮 Admin panel\n\nKerakli bo‘limni tanlang:")
        buttons = [button["text"] for row in panel["reply_markup"]["inline_keyboard"] for button in row]
        self.assertNotIn("📊 Statistika", buttons)
        self.assertIn("🧠 AI roli", buttons)

    def test_owner_panel_shows_owner_tools_but_premium_panel_does_not(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/admin"}}))
        owner_buttons = [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("💎 VIP boshqaruvi", owner_buttons)
        self.assertIn("📢 Kanal boshqaruvi", owner_buttons)
        self.assertIn("✉️ Xabar yuborish", owner_buttons)
        bot.store.grant_premium(1241, time.time() + 86400, "test")
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1241}, "from": {"id": 1241}, "text": "/admin"}}))
        premium_buttons = [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertNotIn("💎 VIP boshqaruvi", premium_buttons)
        self.assertNotIn("📢 Kanal boshqaruvi", premium_buttons)
        self.assertNotIn("✉️ Xabar yuborish", premium_buttons)

    def test_owner_can_grant_vip_from_panel(self) -> None:
        bot = self._bot()
        owner = {"id": 8645314130}
        asyncio.run(bot.process_update({"callback_query": {"id": "vip-menu", "from": owner, "data": "owner:vip", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "vip-grant", "from": owner, "data": "vip:grant", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 11, "chat": {"id": 8645314130}, "from": owner, "text": "1245"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 12, "chat": {"id": 8645314130}, "from": owner, "text": "30"}}))
        self.assertTrue(bot.store.has_premium(1245))

    def test_non_owner_cannot_use_owner_tools(self) -> None:
        bot = self._bot()
        user = {"id": 1246}
        bot.store.grant_premium(1246, time.time() + 86400, "test")
        asyncio.run(bot.process_update({"callback_query": {"id": "owner-tool", "from": user, "data": "owner:vip", "message": {"chat": {"id": 1246}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.callback_answers[-1], ("owner-tool", "Siz admin emassiz.", True))

    def test_non_owner_cannot_use_channel_or_broadcast_tools(self) -> None:
        bot = self._bot()
        user = {"id": 1246}
        bot.store.grant_premium(1246, time.time() + 86400, "test")
        for callback_id, callback_data in (("channel-forged", "owner:channels"), ("broadcast-forged", "owner:broadcast")):
            asyncio.run(bot.process_update({"callback_query": {"id": callback_id, "from": user, "data": callback_data, "message": {"chat": {"id": 1246}, "message_id": 10}}}))
            self.assertEqual(bot.telegram.callback_answers[-1], (callback_id, "Siz admin emassiz.", True))

    def test_owner_can_save_channel_and_broadcast_to_vip(self) -> None:
        bot = self._bot()
        owner = {"id": 8645314130}
        bot.store.mark_started(1247)
        bot.store.grant_premium(1247, time.time() + 86400, "test")
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-menu", "from": owner, "data": "owner:channels", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-add", "from": owner, "data": "channel:add", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-public", "from": owner, "data": "channel:type:public", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 11, "chat": {"id": 8645314130}, "from": owner, "text": "@test_channel"}}))
        self.assertEqual(bot.store.list_channels()[0]["chat_id"], "-100123")
        asyncio.run(bot.process_update({"callback_query": {"id": "broadcast-menu", "from": owner, "data": "owner:broadcast", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "broadcast-vip", "from": owner, "data": "broadcast:vip", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "broadcast-vip-text", "from": owner, "data": "broadcast:type:text:vip", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 12, "chat": {"id": 8645314130}, "from": owner, "text": "VIP xabar"}}))
        self.assertTrue(any(item.get("chat_id") == 1247 and item.get("text") == "VIP xabar" for item in bot.telegram.sent))

    def test_owner_can_broadcast_to_one_user_and_forward(self) -> None:
        bot = self._bot()
        owner = {"id": 8645314130}
        asyncio.run(bot.process_update({"callback_query": {"id": "one-menu", "from": owner, "data": "owner:broadcast", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "one-target", "from": owner, "data": "broadcast:one", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 11, "chat": {"id": 8645314130}, "from": owner, "text": "1248"}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "one-forward", "from": owner, "data": "broadcast:type:forward:one", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 12, "chat": {"id": 8645314130}, "from": owner, "text": "Forward source"}}))
        self.assertEqual(bot.telegram.forwarded[-1], (1248, 8645314130, 12))

    def test_owner_can_broadcast_to_selected_channels(self) -> None:
        bot = self._bot()
        owner = {"id": 8645314130}
        bot.store.upsert_channel("-100124", "Channel A", "channel_a")
        bot.store.upsert_channel("-100125", "Channel B", "channel_b")
        asyncio.run(bot.process_update({"callback_query": {"id": "channels-menu", "from": owner, "data": "owner:broadcast", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channels-target", "from": owner, "data": "broadcast:channels", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-select", "from": owner, "data": "broadcast:toggle:-100124", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-send", "from": owner, "data": "broadcast:send_selected", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "channel-text", "from": owner, "data": "broadcast:type:text:channels|-100124", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 11, "chat": {"id": 8645314130}, "from": owner, "text": "Kanal xabari"}}))
        self.assertTrue(any(item.get("chat_id") == "-100124" and item.get("text") == "Kanal xabari" for item in bot.telegram.sent))
        self.assertFalse(any(item.get("chat_id") == "-100125" and item.get("text") == "Kanal xabari" for item in bot.telegram.sent))

    def test_premium_business_message_uses_its_customer_chat(self) -> None:
        bot = self._bot()
        bot.store.grant_premium(1243, time.time() + 86400, "test")
        asyncio.run(bot.process_update({"business_message": {"message_id": 1, "business_connection_id": "bc-1", "chat": {"id": 9876}, "from": {"id": 1243}, "text": "Mijoz savoli"}}))
        self.assertEqual(bot.telegram.sent[-1]["chat_id"], 9876)
        self.assertEqual(bot.telegram.sent[-1]["business_connection_id"], "bc-1")

    def test_premium_private_customer_message_survives_typing_api_error(self) -> None:
        bot = self._bot()
        bot.store.grant_premium(1243, time.time() + 86400, "test")
        async def fail_typing(chat_id, business_connection_id=None):
            from telegram_api import TelegramApiError
            raise TelegramApiError("sendChatAction", "typing is not supported")
        bot.telegram.send_typing = fail_typing
        asyncio.run(bot.process_update({"business_message": {"message_id": 1, "business_connection_id": "bc-1", "chat": {"id": 777}, "from": {"id": 1243}, "text": "Mijoz savoli"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "AI javob")

    def test_removed_myrole_alias_is_not_a_role_command(self) -> None:
        bot = self._bot()
        bot.store.grant_premium(1244, time.time() + 86400, "test")
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1244}, "from": {"id": 1244}, "text": "/myrole"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "AI javob")

    def test_premium_user_does_not_inherit_owner_global_role(self) -> None:
        bot = self._bot()
        bot.store.set_role("owner global role")
        bot.store.grant_premium(1240, time.time() + 86400, "test")
        self.assertIn("owner global role", bot._effective_system_prompt(None))
        self.assertNotIn("owner global role", bot._effective_system_prompt(1240))
        bot.store.set_user_role(1240, "personal premium role")
        self.assertIn("personal premium role", bot._effective_system_prompt(1240))
        self.assertNotIn("owner global role", bot._effective_system_prompt(1240))

    def test_non_premium_user_gets_subscription_invoice_link(self) -> None:
        bot = self._bot()
        user = {"id": 1235, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1235}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1235}, "from": user, "text": "/premium"}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "premium-buy", "from": user, "data": "premium:buy", "message": {"chat": {"id": 1235}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.invoice_links[-1]["amount"], 100)
        self.assertEqual(bot.telegram.invoice_links[-1]["subscription_period"], 2592000)
        self.assertEqual(bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/invoice/test")

    def test_successful_payment_grants_premium(self) -> None:
        bot = self._bot()
        user = {"id": 1236, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 9, "chat": {"id": 1236}, "from": user, "successful_payment": {"currency": "XTR", "total_amount": 100, "invoice_payload": "premium_monthly_100_stars_v1", "subscription_expiration_date": 2000000000, "is_recurring": True, "is_first_recurring": True, "telegram_payment_charge_id": "charge-1236"}}}))
        self.assertTrue(bot.store.has_premium(1236))
        self.assertIn("To‘lov tasdiqlandi", bot.telegram.sent[-1]["text"])

    def test_pre_checkout_is_approved_only_for_known_payload(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"pre_checkout_query": {"id": "pc-1", "invoice_payload": "premium_monthly_100_stars_v1"}}))
        self.assertEqual(bot.telegram.pre_checkout_answers[-1], ("pc-1", True, None))

    def test_non_owner_start_is_immediate_and_does_not_reveal_provider_or_promo(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1230}, "from": {"id": 1230}, "text": "/start"}}))
        start_text = bot.telegram.sent[-1]["text"]
        self.assertIn("Chatbot accountingizga ulangan", start_text)
        buttons = [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("📚 Buyruqlar", buttons)
        self.assertIn("🦉 Qo‘llanma", buttons)
        self.assertNotIn("/premium", start_text)
        self.assertNotIn("Manus", start_text)
        self.assertNotIn("promo", start_text.casefold())
        self.assertNotIn("Mangekyo", start_text)

    def test_owner_start_uses_same_universal_menu(self) -> None:
        bot = self._bot()
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/start"}}))
        buttons = [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("📚 Buyruqlar", buttons)
        self.assertIn("🦉 Qo‘llanma", buttons)

    def test_about_and_main_menu_callbacks_stay_in_one_message(self) -> None:
        bot = self._bot()
        user = {"id": 1242, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1242}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "about-1", "from": user, "data": "menu:about", "message": {"chat": {"id": 1242}, "message_id": 10}}}))
        about_message_count = len(bot.telegram.sent)
        self.assertIn("Bot haqida 🤖", bot.telegram.sent[-1]["text"])
        self.assertNotIn("Neon", bot.telegram.sent[-1]["text"])
        self.assertNotIn("Statistika", bot.telegram.sent[-1]["text"])
        self.assertEqual(bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "menu:home")
        asyncio.run(bot.process_update({"callback_query": {"id": "home-1", "from": user, "data": "menu:home", "message": {"chat": {"id": 1242}, "message_id": 10}}}))
        self.assertIn("Chatbot accountingizga ulangan", bot.telegram.sent[-1]["text"])
        self.assertEqual(bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "📚 Buyruqlar")
        home_message_count = len(bot.telegram.sent)
        asyncio.run(bot.process_update({"callback_query": {"id": "premium-1", "from": user, "data": "premium:status", "message": {"chat": {"id": 1242}, "message_id": 10}}}))
        self.assertEqual(len(bot.telegram.sent), home_message_count + 1)
        self.assertIn("VIP faol emas", bot.telegram.sent[-1]["text"])
        self.assertEqual(bot.telegram.sent[-1]["message_id"], 10)

    def test_commands_and_guide_navigation(self) -> None:
        bot = self._bot()
        user = {"id": 1250}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1250}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "commands", "from": user, "data": "menu:commands", "message": {"chat": {"id": 1250}, "message_id": 10}}}))
        self.assertIn(".help", bot.telegram.sent[-1]["text"])
        self.assertEqual(bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "commands:next")
        asyncio.run(bot.process_update({"callback_query": {"id": "commands-next", "from": user, "data": "commands:next", "message": {"chat": {"id": 1250}, "message_id": 10}}}))
        self.assertIn(".emoji text", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"callback_query": {"id": "commands-back", "from": user, "data": "commands:back", "message": {"chat": {"id": 1250}, "message_id": 10}}}))
        self.assertIn(".help", bot.telegram.sent[-1]["text"])
        asyncio.run(bot.process_update({"callback_query": {"id": "guide", "from": user, "data": "menu:guide", "message": {"chat": {"id": 1250}, "message_id": 10}}}))
        self.assertIn("Chatbotni ulash qo‘llanmasi", bot.telegram.sent[-1]["text"])
        self.assertIn("🦉 Foydalanish qo‘llanmasi", [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row])

    def test_main_channel_username_is_used_in_guide_caption(self) -> None:
        bot = self._bot()
        bot.store.upsert_channel("-100999", "Asosiy", "ekspres", "main", False, True)
        self.assertEqual(bot._main_channel_username(), "@ekspres")
        self.assertIn("@ekspres", bot._guide_caption("Qo‘llanma"))

    def test_owner_can_configure_start_photo_and_guide_video(self) -> None:
        bot = self._bot()
        owner = {"id": 8645314130}
        asyncio.run(bot.process_update({"callback_query": {"id": "media-menu", "from": owner, "data": "owner:media", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "start-upload", "from": owner, "data": "owner:media:set:start", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 11, "chat": {"id": 8645314130}, "from": owner, "photo": [{"file_id": "small"}, {"file_id": "large"}]}}))
        self.assertEqual(bot.store.get_setting("start_media_file_id"), "large")
        asyncio.run(bot.process_update({"callback_query": {"id": "usage-upload", "from": owner, "data": "owner:media:set:usage_guide", "message": {"chat": {"id": 8645314130}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"message": {"message_id": 12, "chat": {"id": 8645314130}, "from": owner, "video": {"file_id": "usage-video"}}}))
        self.assertEqual(bot.store.get_setting("usage_guide_video_file_id"), "usage-video")

    def test_configured_guide_videos_are_sent_with_captions(self) -> None:
        bot = self._bot()
        bot.store.set_setting("connect_guide_video_file_id", "connect-video")
        bot.store.set_setting("usage_guide_video_file_id", "usage-video")
        user = {"id": 1253}
        asyncio.run(bot.process_update({"callback_query": {"id": "guide-video", "from": user, "data": "menu:guide", "message": {"chat": {"id": 1253}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.videos[-1]["video"], "connect-video")
        self.assertIn("Chatbotni ulash qo‘llanmasi", bot.telegram.videos[-1]["caption"])
        asyncio.run(bot.process_update({"callback_query": {"id": "usage-video", "from": user, "data": "guide:usage", "message": {"chat": {"id": 1253}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.videos[-1]["video"], "usage-video")
        self.assertIn("Chatbotdan foydalanish qo‘llanmasi", bot.telegram.videos[-1]["caption"])

    def test_non_owner_cannot_open_or_upload_menu_media(self) -> None:
        bot = self._bot()
        user = {"id": 1251}
        asyncio.run(bot.process_update({"callback_query": {"id": "media-forged", "from": user, "data": "owner:media", "message": {"chat": {"id": 1251}, "message_id": 10}}}))
        self.assertEqual(bot.telegram.callback_answers[-1], ("media-forged", "Siz admin emassiz.", True))
        self.assertIsNone(bot.store.get_admin_session(1251))

    def test_configured_media_is_sent_with_file_id(self) -> None:
        bot = self._bot()
        bot.store.set_setting("start_media_file_id", "start-photo")
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1252}, "from": {"id": 1252}, "text": "/start"}}))
        self.assertEqual(bot.telegram.photos[-1]["photo"], "start-photo")
        self.assertIn("📚 Buyruqlar", [button["text"] for row in bot.telegram.photos[-1]["reply_markup"]["inline_keyboard"] for button in row])

    def test_profile_removes_balance_referral_and_replaces_tariffs_with_vip(self) -> None:
        bot = self._bot()
        user = {"id": 1254}
        asyncio.run(bot.process_update({"callback_query": {"id": "profile", "from": user, "data": "menu:profile", "message": {"chat": {"id": 1254}, "message_id": 10}}}))
        profile = bot.telegram.sent[-1]
        self.assertNotIn("Balansingiz", profile["text"])
        self.assertNotIn("Takliflaringiz", profile["text"])
        self.assertNotIn("Taklif havola", str(profile["reply_markup"]))
        buttons = [button["text"] for row in profile["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("VIP 💎", buttons)
        self.assertNotIn("Pro", buttons)
        self.assertNotIn("Biznes", buttons)

    def test_vip_profile_screen_uses_requested_text_and_payment(self) -> None:
        bot = self._bot()
        user = {"id": 1255}
        asyncio.run(bot.process_update({"callback_query": {"id": "vip-profile", "from": user, "data": "profile:vip", "message": {"chat": {"id": 1255}, "message_id": 10}}}))
        screen = bot.telegram.sent[-1]
        self.assertIn("📩 Avto javoblar: 100 ta", screen["text"])
        self.assertIn("🤖 AI avto javob (kunlik): 500 ta", screen["text"])
        self.assertIn("🧠 «.ai» savol (kunlik): 100 ta", screen["text"])
        self.assertIn("🖼 «.img» / «.rasm» (kunlik): 5 ta", screen["text"])
        self.assertIn("VIP 💎 obuna (cheklovlarsiz)", screen["text"])
        self.assertEqual(screen["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "premium:buy")

    def test_topup_screen_contains_only_stars_payment_and_back(self) -> None:
        bot = self._bot()
        user = {"id": 1256}
        asyncio.run(bot.process_update({"callback_query": {"id": "topup", "from": user, "data": "profile:topup", "message": {"chat": {"id": 1256}, "message_id": 10}}}))
        screen = bot.telegram.sent[-1]
        self.assertNotIn("Joriy balans", screen["text"])
        buttons = [button for row in screen["reply_markup"]["inline_keyboard"] for button in row]
        self.assertEqual([button["text"] for button in buttons], ["⭐ Avto to‘lov (stars)", "🔙 Orqaga"])
        self.assertEqual(buttons[0]["callback_data"], "premium:buy")

    def test_settings_screen_has_telegram_settings_url_and_back(self) -> None:
        bot = self._bot()
        user = {"id": 1257}
        asyncio.run(bot.process_update({"callback_query": {"id": "settings", "from": user, "data": "menu:settings", "message": {"chat": {"id": 1257}, "message_id": 10}}}))
        screen = bot.telegram.sent[-1]
        self.assertTrue(screen["text"].startswith("@InfoUchihaBot sozlamalari ⚙️"))
        self.assertIn("Qisqa qo‘llanma", screen["text"])
        self.assertIn("Buyruqlar ruxsati", screen["text"])
        url_button = screen["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(url_button["text"], "🤝 Chatbotni sozlash")
        self.assertEqual(url_button["url"], "tg://settings/edit")
        self.assertEqual(screen["reply_markup"]["inline_keyboard"][-1][0]["callback_data"], "menu:home")

    def test_profile_vip_back_returns_to_profile(self) -> None:
        bot = self._bot()
        user = {"id": 1258}
        asyncio.run(bot.process_update({"callback_query": {"id": "vip", "from": user, "data": "profile:vip", "message": {"chat": {"id": 1258}, "message_id": 10}}}))
        asyncio.run(bot.process_update({"callback_query": {"id": "vip-back", "from": user, "data": "profile:home", "message": {"chat": {"id": 1258}, "message_id": 10}}}))
        self.assertIn("Profil", bot.telegram.sent[-1]["text"])
        self.assertIn("VIP 💎", [button["text"] for row in bot.telegram.sent[-1]["reply_markup"]["inline_keyboard"] for button in row])

    def test_required_subscription_keyboard_is_numbered(self) -> None:
        bot = self._bot()
        channels = [
            {"chat_id": "-1001", "title": "A", "username": "a", "channel_type": "required"},
            {"chat_id": "-1002", "title": "B", "username": "b", "channel_type": "required"},
        ]
        keyboard = bot._subscription_gate_keyboard(channels)
        buttons = keyboard["inline_keyboard"]
        self.assertEqual(buttons[0][0]["text"], "💠 1-kanal")
        self.assertEqual(buttons[1][0]["text"], "💠 2-kanal")
        self.assertEqual(buttons[-1][0]["text"], "Tekshirish ✅")
        self.assertEqual(buttons[-1][0]["callback_data"], "subscription:check")

    def test_promo_inquiries_are_silent(self) -> None:
        bot = self._bot()
        user = {"id": 1237, "is_bot": False}
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 1237}, "from": user, "text": "/start"}}))
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 1237}, "from": user, "text": "Promo kod bormi?"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "So‘rov bajarilmadi.")
        self.assertNotIn("Mangekyo", bot.telegram.sent[-1]["text"])

    def test_owner_id_is_fixed_even_if_settings_contains_another_admin(self) -> None:
        bot = self._bot()
        bot.settings.admin_user_id = 123
        asyncio.run(bot.process_update({"message": {"message_id": 1, "chat": {"id": 123}, "from": {"id": 123}, "text": "/admin"}}))
        self.assertEqual(bot.telegram.sent[-1]["text"], "Siz admin emassiz.")
        asyncio.run(bot.process_update({"message": {"message_id": 2, "chat": {"id": 8645314130}, "from": {"id": 8645314130}, "text": "/admin"}}))
        self.assertIn("Admin panel", bot.telegram.sent[-1]["text"])

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
    def test_owner_business_message_is_never_answered_by_ai(self) -> None:
        bot = self._bot()
        bot._set_manual_pause_enabled(False)
        owner_message = {"message_id": 50, "business_connection_id": "bc-1", "chat": {"id": 777}, "from": {"id": 8645314130}, "text": "Owner yozgan xabar"}
        asyncio.run(bot.process_update({"business_message": owner_message}))
        self.assertEqual(bot.telegram.sent, [])

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
    def test_photo_and_video_send_payloads(self) -> None:
        bot = TelegramBotApi("dummy")
        captured = []

        async def fake_call(method, payload):
            captured.append((method, payload))
            return {"message_id": 1}

        bot.call = fake_call  # type: ignore[method-assign]
        asyncio.run(bot.send_photo(1, "photo-file", "caption", reply_markup={"inline_keyboard": []}))
        asyncio.run(bot.send_video(1, "video-file", "caption", reply_markup={"inline_keyboard": []}))
        self.assertEqual(captured[0][0], "sendPhoto")
        self.assertEqual(captured[0][1]["photo"], "photo-file")
        self.assertEqual(captured[1][0], "sendVideo")
        self.assertEqual(captured[1][1]["video"], "video-file")

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
