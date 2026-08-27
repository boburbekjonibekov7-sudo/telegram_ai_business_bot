from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import signal
from collections import defaultdict
from typing import Any

from ai_providers import AIService, ProviderError
from config import Settings
from storage import JsonStore
from telegram_api import TelegramApiError, TelegramBotApi
from postgres_store import PostgresStore


LOGGER = logging.getLogger("telegram_ai_business_bot")
OWNER_ADMIN_ID = 8645314130
OWNER_PAUSE_SECONDS = 30 * 60
STAR_SUBSCRIPTION_AMOUNT = 100
STAR_SUBSCRIPTION_PERIOD_SECONDS = 30 * 24 * 60 * 60
STAR_SUBSCRIPTION_PAYLOAD = "premium_monthly_100_stars_v1"
MANGEKYO_PROMO_CODE = "mangenkyo sharingan"
MANGEKYO_PROMO_REPLY = "Sharingan faollashdi!\nEndi siz botdan 1 oy bepul foydalanasiz!!!\n/start /start /start"
PROMO_SILENT_REPLY = "So‘rov bajarilmadi."
START_MENU_TEXT_TEMPLATE = "🤖 Salom, {name} 🫡\n\n💬 Chatbot accountingizga ulangan — sizga yozadigan odamlarga avto javob beradi.\n\n❓ Quyidagi tugmalar orqali buyruqlar, avto javob va sozlamalarni boshqaring ⚙️"
COMMANDS_PAGE_1_TEXT = """🤖 Chatbot buyruqlari:

.help — 📖 ChatBot dan foydalanish qo‘llanmasi!
.ping — 🚀 ChatBot tezligi!
.settings — ⚙️ ChatBot sozlamalari!
.add — ➕ Avto javob qo‘shish!
.list — 💬 Avto javoblarni ko‘rish!
.info — 👥 Ikki tomon haqida ma’lumot!
.type text — 📝 Harfma-harf yozish animatsiyasi!
.ai text — 🤖 AI ga savol berish!
.send matn @kim 5 — 📤 Accountingizdan xabar yuborish!
.soat — ⏱ Profilga nikga soat qo‘yish!
.online — 🟢 24 soat online rejimini yoqish!
.offline — 🟢 24 soat online rejimini o‘chirish!"""
COMMANDS_PAGE_2_TEXT = """🤖 Chatbot buyruqlari — davom:

.emoji text — Matnni RANDOM premium emoji qilish!
.dice — 🎟 🎯 🍭 🎰 ⚽ 🏀 har safar har xil yuboriladi!
.dice1 — 🎟 yuborish!
.dice2 — 🎯 yuborish!
.dice3 — 🍭 yuborish!
.dice4 — 🎰 yuborish!
.dice5 — ⚽ yuborish!
.dice6 — 🏀 yuborish!"""
GUIDE_CONNECT_CAPTION = "🤖 Chatbotni ulash qo‘llanmasi"
GUIDE_USAGE_CAPTION = "🤖 Chatbotdan foydalanish qo‘llanmasi"
AUTO_REPLY_COMMANDS = ("help", "ping", "ai", "down", "music", "type", "emoji", "dice", "checklist", "info")
VIP_FEATURES_TEXT = """📩 Avto javoblar: 100 ta
🤖 AI avto javob (kunlik): 500 ta
🧠 «.ai» savol (kunlik): 100 ta
🖼 «.img» / «.rasm» (kunlik): 5 ta

✅ Bepuldagi hamma narsa
🕔 Profilga soat qo'yish
🟢 24/7 online rejimi
✍️ «.ok» / «.loc» uchun maxsus so'z
💬 Hammaga AI avto javob berish
🎨 Avto javobga rasm/sticker/ovoz/video/gif
🔗 <<.img>>, <<.rasm>>, <<.story>>

VIP 💎 obuna (cheklovlarsiz)"""
BOT_ABOUT_TEXT = "Bot haqida 🤖\n\n• Telegram Business va Chat Automation chatlariga AI javob beradi.\n• Business chatda yuborilgan APK fayllarni avtomatik o‘chirishni qo‘llab-quvvatlaydi.\n\nVIP 💎 imkoniyatlari:\n• Oyiga 100 Telegram Stars evaziga 30 kunlik access.\n• Shaxsiy AI chat va shaxsiy rol sozlamalari.\n• Kengaytirilgan admin panel va pause boshqaruvi.\n• To‘lovdan keyin VIP funksiyalar avtomatik ochiladi."
VIP_LABEL = "VIP"
MEDIA_SLOTS = {
    "start": ("start_media_file_id", "start_media_type", "Start rasmi", "photo"),
    "commands": ("commands_media_file_id", "commands_media_type", "Buyruqlar rasmi", "photo"),
    "connect_guide": ("connect_guide_video_file_id", "connect_guide_media_type", "Chatbotni ulash videosi", "video"),
    "usage_guide": ("usage_guide_video_file_id", "usage_guide_media_type", "Foydalanish qo‘llanmasi videosi", "video"),
}
MEDIA_SESSION_SLOTS = {
    "media:set:start": "start",
    "media:set:commands": "commands",
    "media:set:connect_guide": "connect_guide",
    "media:set:usage_guide": "usage_guide",
}




class BusinessAiBot:
    def __init__(self, settings: Settings, store: Any | None = None):
        self.settings = settings
        self.telegram = TelegramBotApi(settings.bot_token)
        self.ai = AIService(settings)
        self.store = store or PostgresStore.from_env(settings.max_history_messages) or JsonStore(settings.data_dir, settings.max_history_messages)
        self.pause_store = None
        self.connections: dict[str, dict[str, Any]] = {}
        # Har bir Business connection o‘z user profili va sozlamalari bilan ishlaydi.
        # Global admin huquqi faqat loyiha egasining hardcoded Telegram ID'siga tegishli.
        self.admin_user_ids: set[int] = {OWNER_ADMIN_ID}
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
        if "pre_checkout_query" in update:
            await self.handle_pre_checkout_query(update["pre_checkout_query"])
            return
        if "subscription" in update:
            await self.handle_subscription_update(update["subscription"])
            return
        if "business_connection" in update:
            self._cache_connection(update["business_connection"])
            return
        if "callback_query" in update:
            await self.handle_callback_query(update["callback_query"])
            return
        if "business_message" in update:
            message = update["business_message"]
            LOGGER.info(
                "Business message qabul qilindi: chat_id=%s connection_id=%s",
                (message.get("chat") or {}).get("id"),
                message.get("business_connection_id"),
            )
            await self.handle_message(message, is_business=True)
            return
        if "edited_business_message" in update:
            await self._handle_edited_business_message(update["edited_business_message"])
            return
        if "deleted_business_messages" in update:
            await self._handle_deleted_business_messages(update["deleted_business_messages"])
            return
        if "message" in update:
            message = update["message"]
            if message.get("successful_payment"):
                await self.handle_successful_payment(message)
                return
            await self.handle_message(message, is_business=False)

    async def _business_owner_id(self, connection_id: str) -> int | None:
        connection = await self._get_connection(connection_id)
        owner_id = (connection or {}).get("user", {}).get("id")
        return owner_id if isinstance(owner_id, int) else None

    @staticmethod
    def _event_time(value: Any) -> str:
        timestamp = value if isinstance(value, int) else int(time.time())
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))

    async def _send_event_notice(self, owner_id: int, connection_id: str, chat_id: int, text: str, destination: str) -> None:
        if destination == "chat":
            await self._send_chunks(chat_id, text, connection_id, None)
        else:
            await self._send_chunks(owner_id, text, None, None)

    async def _handle_edited_business_message(self, message: dict[str, Any]) -> None:
        connection_id = str(message.get("business_connection_id") or "")
        chat_id = self._chat_id(message)
        message_id = message.get("message_id")
        if not connection_id or not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        owner_id = await self._business_owner_id(connection_id)
        if not isinstance(owner_id, int):
            return
        if self._user_id(message) == owner_id:
            return
        if self._user_setting(owner_id, "edit_notify_enabled", "0") != "1":
            return
        cached = self._cached_business_message(owner_id, connection_id, chat_id, message_id) or {}
        old_text = str(cached.get("text") or "(oldingi xabar matni mavjud emas)")
        new_text = self._message_text(message) or "(matnsiz xabar)"
        destination = self._user_setting(owner_id, "edit_notify_destination", "chat")
        message_type = self._user_setting(owner_id, "edit_notify_type", "notification")
        show_time = self._user_setting(owner_id, "edit_notify_timestamp", "0") == "1"
        if message_type == "copy":
            notice = new_text
        else:
            notice = f"👤 Suhbatdoshingiz xabarini tahrirladi ✏️\n\n💬 {old_text}\n\n✏️ {new_text}"
        if show_time:
            notice += f"\n\n🕔 Yuborilgan vaqt: {self._event_time(cached.get('date'))}\n🕔 Tahrirlangan vaqt: {self._event_time(message.get('edit_date'))}"
        await self._send_event_notice(owner_id, connection_id, chat_id, notice, destination)
        self._remember_business_message(owner_id, message)

    async def _handle_deleted_business_messages(self, update: dict[str, Any]) -> None:
        connection_id = str(update.get("business_connection_id") or "")
        chat_id = self._chat_id(update)
        message_ids = update.get("message_ids")
        if not connection_id or not isinstance(chat_id, int) or not isinstance(message_ids, list):
            return
        owner_id = await self._business_owner_id(connection_id)
        if not isinstance(owner_id, int):
            return
        if self._user_setting(owner_id, "delete_notify_enabled", "1") != "1":
            return
        destination = self._user_setting(owner_id, "delete_notify_destination", "bot")
        message_type = self._user_setting(owner_id, "delete_notify_type", "notification")
        show_time = self._user_setting(owner_id, "delete_notify_timestamp", "0") == "1"
        for raw_message_id in message_ids[:100]:
            if not isinstance(raw_message_id, int):
                continue
            cached = self._cached_business_message(owner_id, connection_id, chat_id, raw_message_id) or {}
            if cached.get("sender_id") == owner_id:
                continue
            old_text = str(cached.get("text") or "(o‘chirilgan xabar matni mavjud emas)")
            if message_type == "copy":
                notice = old_text
            else:
                notice = f"👤 Suhbatdoshingiz xabarini o‘chirdi 🗑\n\n💬 {old_text}"
            if show_time:
                notice += f"\n\n🕔 Yuborilgan vaqt: {self._event_time(cached.get('date'))}\n🕔 O‘chirilgan vaqt: {self._event_time(update.get('date'))}"
            await self._send_event_notice(owner_id, connection_id, chat_id, notice, destination)
            self._forget_business_message(owner_id, connection_id, chat_id, raw_message_id)

    def _cache_connection(self, connection: dict[str, Any]) -> None:
        connection_id = connection.get("id")
        if not connection_id:
            return
        self.connections[str(connection_id)] = connection
        connection_user_id = (connection.get("user") or {}).get("id")
        profile_writer = getattr(self.store, "upsert_business_profile", None)
        if isinstance(connection_user_id, int) and callable(profile_writer):
            profile_writer(str(connection_id), connection_user_id)
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
    def _user_id(message: dict[str, Any]) -> int | None:
        user_id = (message.get("from") or {}).get("id")
        return user_id if isinstance(user_id, int) else None

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        text = message.get("text") or message.get("caption") or ""
        return str(text).strip()

    def _message_cache(self, owner_id: int) -> dict[str, dict[str, Any]]:
        raw = self._user_setting(owner_id, "business_message_cache", "{}")
        try:
            loaded = json.loads(raw)
            return loaded if isinstance(loaded, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _remember_business_message(self, owner_id: int, message: dict[str, Any]) -> None:
        message_id = message.get("message_id")
        connection_id = str(message.get("business_connection_id") or "")
        chat_id = self._chat_id(message)
        if not isinstance(message_id, int) or not connection_id or not isinstance(chat_id, int):
            return
        cache = self._message_cache(owner_id)
        cache[f"{connection_id}:{chat_id}:{message_id}"] = {
            "text": self._message_text(message),
            "sender_id": (message.get("from") or {}).get("id") if isinstance((message.get("from") or {}).get("id"), int) else None,
            "date": message.get("date") if isinstance(message.get("date"), int) else int(time.time()),
            "chat_id": chat_id,
            "connection_id": connection_id,
            "message_id": message_id,
        }
        if len(cache) > 100:
            cache = dict(list(cache.items())[-100:])
        self._set_user_setting(owner_id, "business_message_cache", json.dumps(cache, ensure_ascii=False, separators=(",", ":")))

    def _cached_business_message(self, owner_id: int, connection_id: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
        return self._message_cache(owner_id).get(f"{connection_id}:{chat_id}:{message_id}")

    def _forget_business_message(self, owner_id: int, connection_id: str, chat_id: int, message_id: int) -> None:
        cache = self._message_cache(owner_id)
        cache.pop(f"{connection_id}:{chat_id}:{message_id}", None)
        self._set_user_setting(owner_id, "business_message_cache", json.dumps(cache, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _normalized_text(text: str) -> str:
        return " ".join(text.casefold().split())

    @classmethod
    def _is_promo_trigger(cls, text: str) -> bool:
        return cls._normalized_text(text) == MANGEKYO_PROMO_CODE

    @classmethod
    def _is_promo_inquiry(cls, text: str) -> bool:
        normalized = cls._normalized_text(text)
        if normalized == MANGEKYO_PROMO_CODE:
            return False
        return any(term in normalized for term in ("promo", "promokod", "promo code", "mangekyo", "sharingan"))

    def _effective_system_prompt(self, user_id: int | None = None, business_connection_id: str | None = None) -> str:
        # Har bir premium user yoki Business connection o‘z roliga ega.
        # Global role faqat eski owner profiliga tegishli; boshqa profillarga meros qilinmaydi.
        role = ""
        if user_id == OWNER_ADMIN_ID or (user_id is None and business_connection_id is None):
            role = self.store.get_role("")
        elif isinstance(user_id, int):
            user_role_method = getattr(self.store, "get_user_role", None)
            if callable(user_role_method):
                role = user_role_method(user_id, "")
        elif business_connection_id:
            business_role_method = getattr(self.store, "get_business_role", None)
            if callable(business_role_method):
                role = business_role_method(business_connection_id, "")
        if not role:
            return self.settings.system_prompt
        return f"{self.settings.system_prompt}\n\nQo‘shimcha boshqaruvchi roli:\n{role}"

    def _is_admin(self, message: dict[str, Any]) -> bool:
        sender = message.get("from") or {}
        user_id = sender.get("id")
        return isinstance(user_id, int) and user_id in self.admin_user_ids

    def _required_channels(self) -> list[dict[str, Any]]:
        getter = getattr(self.store, "required_channels", None)
        channels = getter() if callable(getter) else []
        return [channel for channel in channels if isinstance(channel, dict) and str(channel.get("channel_type") or "") != "url"]

    @staticmethod
    def _channel_join_url(channel: dict[str, Any]) -> str | None:
        invite_link = str(channel.get("invite_link") or "").strip()
        if invite_link:
            return invite_link
        username = str(channel.get("username") or "").strip()
        if username:
            return username if username.startswith("http") else f"https://t.me/{username.lstrip('@')}"
        return None

    def _subscription_gate_keyboard(self, channels: list[dict[str, Any]]) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        for index, channel in enumerate(channels, start=1):
            url = self._channel_join_url(channel)
            button: dict[str, str] = {"text": f"💠 {index}-kanal"}
            if url:
                button["url"] = url
            else:
                button["callback_data"] = f"subscription:channel:{index}"
            rows.append([button])
        rows.append([{"text": "Tekshirish ✅", "callback_data": "subscription:check"}])
        return {"inline_keyboard": rows}

    def _subscription_gate_text(self, channels: list[dict[str, Any]]) -> str:
        return "Botdan foydalanish uchun quyidagi kanal(lar)ga obuna yoki zayavka tashlang va Tekshirish ✅ tugmasini bosing!"

    async def _is_subscription_satisfied(self, user_id: int | None) -> bool:
        if user_id is None or user_id == OWNER_ADMIN_ID:
            return True
        channels = self._required_channels()
        if not channels:
            return True
        for channel in channels:
            chat_id = str(channel.get("chat_id") or "")
            if not chat_id:
                continue
            try:
                member = await self.telegram.get_chat_member(chat_id, user_id)
                status = str(member.get("status") or "")
                if status in {"member", "administrator", "creator"} or (status == "restricted" and member.get("is_member") is True):
                    continue
            except TelegramApiError as exc:
                LOGGER.warning("Majburiy obuna membership tekshiruvi xatosi chat=%s: %s", chat_id, exc)
            try:
                requests = await self.telegram.get_chat_join_requests(chat_id, user_id, str(channel.get("invite_link") or "") or None, 1)
                if any(isinstance(request, dict) and isinstance((request.get("user") or {}).get("id"), int) and (request.get("user") or {}).get("id") == user_id for request in requests):
                    continue
            except TelegramApiError as exc:
                LOGGER.warning("Join request tekshiruvi xatosi chat=%s: %s", chat_id, exc)
            return False
        return True

    async def _ensure_subscription_or_prompt(self, chat_id: int, user_id: int | None, reply_to: int | None = None, edit_message_id: int | None = None) -> bool:
        if await self._is_subscription_satisfied(user_id):
            return True
        channels = self._required_channels()
        text = self._subscription_gate_text(channels)
        markup = self._subscription_gate_keyboard(channels)
        if edit_message_id is not None:
            try:
                await self.telegram.edit_message_text(chat_id, edit_message_id, text, markup)
            except TelegramApiError:
                await self._send_chunks(chat_id, text, None, reply_to, markup)
        else:
            await self._send_chunks(chat_id, text, None, reply_to, markup)
        return False

    async def _handle_admin_command(
        self,
        message: dict[str, Any],
        text: str,
        chat_id: int,
    ) -> bool:
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) == 2 else ""
        reply_to = message.get("message_id")

        sender_id = self._user_id(message)
        if command == "/start":
            if sender_id is not None:
                marker = getattr(self.store, "mark_started", None)
                if callable(marker):
                    marker(sender_id)
            if not await self._ensure_subscription_or_prompt(chat_id, sender_id, reply_to):
                return True
            await self._send_start_screen(chat_id, reply_to, user=message.get("from"))
            return True

        if sender_id != OWNER_ADMIN_ID and not await self._ensure_subscription_or_prompt(chat_id, sender_id, reply_to):
            return True

        if command in {"/terms", "/shartlar"}:
            await self._send_chunks(chat_id, "Premium xizmat: oyiga 100 Telegram Stars, muddati 30 kun. To‘lov muvaffaqiyatli tasdiqlangandan keyin premium funksiyalar ochiladi. Bekor qilish yoki to‘lov bo‘yicha yordam uchun /paysupport buyrug‘idan foydalaning.", None, reply_to)
            return True

        if command in {"/paysupport", "/support"}:
            await self._send_chunks(chat_id, "To‘lov yoki premium access muammosi bo‘lsa, bot egasiga shu chatda yozing. To‘lovni tekshirish uchun invoice ma’lumotlarini yuboring.", None, reply_to)
            return True

        if command == "/premium":
            await self._send_premium_panel(chat_id, self._user_id(message), reply_to)
            return True


        if command == "/id":
            sender_id = (message.get("from") or {}).get("id")
            await self._send_chunks(chat_id, f"Sizning Telegram user ID: {sender_id}", None, reply_to)
            return True

        sender_id = self._user_id(message)
        is_owner = sender_id == OWNER_ADMIN_ID
        is_premium = sender_id is not None and self._has_premium(sender_id)
        if command == "/admin":
            if not is_owner and not is_premium:
                await self._send_chunks(chat_id, "Siz admin emassiz.", None, reply_to)
                return True
            await self._send_chunks(chat_id, self._admin_panel_text(), None, reply_to, self._admin_panel_keyboard(include_statistics=is_owner, include_main_menu=True, user_id=sender_id, include_owner_tools=is_owner))
            return True
        if command != "/rol":
            return False
        if not is_owner and not is_premium:
            await self._send_chunks(chat_id, "Siz admin emassiz.", None, reply_to)
            return True
        if not is_owner:
            await self._handle_premium_role(message, argument, chat_id, reply_to)
            return True
        if not argument:
            current = self.store.get_role("")
            status = current if current else "AI oddiy javob rejimida ishlaydi; qo‘shimcha rol berilmagan."
            await self._send_chunks(chat_id, f"Joriy rol:\n{status}", None, reply_to)
            return True
        if argument.lower() in {"reset", "default", "tozalash"}:
            self.store.clear_role()
            await self._send_chunks(chat_id, "Global qo‘shimcha rol olib tashlandi; AI oddiy javob rejimida ishlaydi.", None, reply_to)
            return True
        if len(argument) > 2000:
            await self._send_chunks(chat_id, "Rol 2000 belgidan oshmasligi kerak.", None, reply_to)
            return True
        self.store.set_role(argument)
        await self._send_chunks(chat_id, "Yangi rol saqlandi. Keyingi Business xabarlar shu uslubda javoblanadi.", None, reply_to)
        return True

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
        user_id = self._user_id(message)
        if chat_id is None:
            return

        business_connection_id = message.get("business_connection_id") if is_business else None
        business_owner_id: int | None = None
        if is_business:
            if not isinstance(business_connection_id, str) or not business_connection_id:
                LOGGER.warning("Business message’da connection_id yo‘q; javob yuborilmadi")
                return
            connection = await self._get_connection(business_connection_id)
            if not connection or connection.get("is_enabled") is False:
                LOGGER.warning("Business ulanish faol emas: %s", business_connection_id)
                return
            connection_user = connection.get("user") or {}
            if isinstance(connection_user.get("id"), int):
                business_owner_id = connection_user["id"]
            rights = connection.get("rights") or {}
            if rights.get("can_reply") is not True:
                LOGGER.warning(
                    "Business botda can_reply huquqi yo‘q: %s; Chat Automation’da reply/send messages huquqini yoqing",
                    business_connection_id,
                )
                return
            if self._is_apk_message(message) and user_id != business_owner_id and isinstance(business_owner_id, int):
                if self._user_setting(business_owner_id, "settings_apk_delete_enabled", "1") == "1":
                    await self._delete_business_apk(message, business_connection_id)
                    return
            if isinstance(business_owner_id, int) and user_id != business_owner_id:
                self._remember_business_message(business_owner_id, message)

        text = self._message_text(message)
        owner_session = self._owner_session(user_id) if not is_business and user_id == OWNER_ADMIN_ID else None
        user_session = self._user_session(user_id) if not is_business and isinstance(user_id, int) else None
        if not text and not (owner_session and owner_session.get("state") in {"broadcast_forward", "media_upload"}):
            return
        if not is_business and await self._handle_admin_command(message, text, chat_id):
            return
        if not is_business and user_id == OWNER_ADMIN_ID and await self._handle_owner_session(message, text, chat_id):
            return
        if not is_business and isinstance(user_id, int) and await self._handle_auto_reply_session(message, text, chat_id, user_session):
            return

        if not is_business and user_id != OWNER_ADMIN_ID and not await self._ensure_subscription_or_prompt(chat_id, user_id, message.get("message_id")):
            return

        if not is_business and user_id != OWNER_ADMIN_ID and self._is_promo_trigger(text):
            await self._handle_mangekyo_promo(message, chat_id)
            return
        if not is_business and user_id != OWNER_ADMIN_ID and self._is_promo_inquiry(text):
            await self._send_chunks(chat_id, PROMO_SILENT_REPLY, None, message.get("message_id"))
            return

        if not is_business and user_id != OWNER_ADMIN_ID and not self._has_premium(user_id):
            await self._send_premium_panel(chat_id, user_id, message.get("message_id"))
            return

        storage_key = self._storage_key(chat_id, business_connection_id)
        pause_seconds = max(1, int(getattr(self.settings, "manual_pause_seconds", OWNER_PAUSE_SECONDS)))
        pause_enabled = self._manual_pause_enabled(business_owner_id) if is_business else False
        if is_business and (business_owner_id == user_id or (business_owner_id is None and self._is_admin(message))):
            if pause_enabled:
                self._mark_owner_activity(storage_key)
                LOGGER.info("Owner qo‘lda xabar yubordi; chat %s soniyaga pauzaga qo‘yildi: %s", pause_seconds, storage_key)
            else:
                LOGGER.info("Owner Business xabari AI javobisiz qoldirildi: %s", storage_key)
            return
        if is_business and pause_enabled:
            remaining = self._owner_pause_remaining(storage_key, pause_seconds)
            if remaining > 0:
                LOGGER.info(
                    "Avtomatik javob pauzada: %s, qolgan soniya=%s",
                    storage_key,
                    remaining,
                )
                return

        async with self.chat_locks[storage_key]:
            reply_owner_id = business_owner_id if is_business else user_id
            if isinstance(reply_owner_id, int):
                finder = getattr(self.store, "find_auto_reply", None)
                try:
                    custom_reply = finder(reply_owner_id, text) if callable(finder) else None
                except Exception as exc:
                    LOGGER.warning("Auto-reply trigger qidirilmadi: %s", exc)
                    custom_reply = None
                if isinstance(custom_reply, dict):
                    reply_to = message.get("message_id") if custom_reply.get("reply_in_message") else None
                    await self._send_chunks(chat_id, str(custom_reply.get("response") or ""), business_connection_id, reply_to)
                    if is_business and custom_reply.get("reply_to_owner") and isinstance(business_owner_id, int):
                        await self._send_chunks(business_owner_id, f"📩 Avto javob yuborildi.\n\nTrigger: {custom_reply.get('trigger')}\nJavob: {custom_reply.get('response')}", None, None)
                    return
            if await self._handle_builtin_auto_command(message, text, chat_id, is_business, business_owner_id):
                return
            if text.lower() in {"/reset", "/clear"}:
                self.store.clear(storage_key)
                await self._send_chunks(
                    chat_id,
                    "Suhbat tarixi tozalandi.",
                    business_connection_id,
                    message.get("message_id"),
                )
                return

            history = self.store.history(
                storage_key,
                self._effective_system_prompt(
                    user_id if not is_business else business_owner_id,
                    business_connection_id if is_business else None,
                ),
            )
            history.append({"role": "user", "content": text})
            try:
                try:
                    if not is_business or not isinstance(business_owner_id, int) or self._user_setting(business_owner_id, "settings_typing_enabled", "1") == "1":
                        await self.telegram.send_typing(chat_id, business_connection_id)
                except TelegramApiError as exc:
                    # Typing is cosmetic; a Telegram limitation must not block the AI reply.
                    LOGGER.warning("Typing action yuborilmadi, AI javobi davom etadi: %s", exc)
                answer, provider_name = await self.ai.answer(history)
            except ProviderError as exc:
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
            if is_business and isinstance(business_owner_id, int) and self._user_setting(business_owner_id, "settings_read_enabled", "1") == "1":
                read_method = getattr(self.telegram, "read_business_message", None)
                if callable(read_method) and isinstance(message.get("message_id"), int):
                    try:
                        await read_method(business_connection_id, message["message_id"])
                    except TelegramApiError as exc:
                        LOGGER.warning("Business xabarini o‘qilgan deb belgilab bo‘lmadi: %s", exc)

    def _mark_owner_activity(self, key: str) -> None:
        self.store.mark_owner_activity(key)
        if self.pause_store is not None:
            self.pause_store.mark_owner_activity(key)

    def _owner_pause_remaining(self, key: str, pause_seconds: int) -> int:
        remaining = self.store.owner_pause_remaining(key, pause_seconds)
        if self.pause_store is not None:
            remaining = max(remaining, self.pause_store.owner_pause_remaining(key, pause_seconds))
        return remaining

    async def handle_callback_query(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from") or {}
        user_id = sender.get("id")
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = self._chat_id(message)
        message_id = message.get("message_id")
        is_owner = isinstance(user_id, int) and user_id == OWNER_ADMIN_ID
        is_premium = isinstance(user_id, int) and self._has_premium(user_id)
        if data == "subscription:check":
            await self.telegram.answer_callback_query(callback_id)
            if await self._is_subscription_satisfied(user_id):
                if isinstance(chat_id, int):
                    await self._send_start_screen(chat_id, edit_message_id=message_id if isinstance(message_id, int) else None, user=sender)
            else:
                channels = self._required_channels()
                await self._edit_owner_screen(chat_id, message_id, self._subscription_gate_text(channels), self._subscription_gate_keyboard(channels))
            return
        if user_id != OWNER_ADMIN_ID and not await self._is_subscription_satisfied(user_id):
            await self.telegram.answer_callback_query(callback_id, "Avval kanalga obuna bo‘ling yoki zayavka yuboring.", True)
            return
        owner_only_callback = data.startswith(("owner:", "vip:", "channel:", "broadcast:"))
        if owner_only_callback and not is_owner:
            await self.telegram.answer_callback_query(callback_id, "Siz admin emassiz.", True)
            return
        if data.startswith("admin:") and not (is_owner or is_premium):
            await self.telegram.answer_callback_query(callback_id, "Siz admin emassiz.", True)
            return
        if data == "admin:stats" and not is_owner:
            await self.telegram.answer_callback_query(callback_id, "Bu bo‘lim faqat owner uchun.", True)
            return
        toggle_callbacks = {
            "settings:edit:toggle", "settings:edit:time", "settings:delete:toggle", "settings:delete:time",
            "settings:apk", "settings:typing", "settings:read", "settings:currency",
        }
        if data not in toggle_callbacks and not data.startswith("auto:toggle:"):
            await self.telegram.answer_callback_query(callback_id)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        if data == "owner:vip":
            await self._edit_owner_screen(chat_id, message_id, self._owner_vip_text(), self._owner_vip_keyboard())
            return
        if data == "vip:list":
            await self._edit_owner_screen(chat_id, message_id, self._owner_vip_text(), self._owner_vip_keyboard())
            return
        if data == "vip:grant":
            self._set_owner_session(user_id, "vip_grant_id")
            await self._edit_owner_screen(chat_id, message_id, "➕ VIP berish\n\nUserning Telegram ID sini yuboring:", self._owner_vip_keyboard())
            return
        if data == "vip:revoke":
            self._set_owner_session(user_id, "vip_revoke_id")
            await self._edit_owner_screen(chat_id, message_id, "❌ VIP olish\n\nUserning Telegram ID sini yuboring:", self._owner_vip_keyboard())
            return
        if data == "owner:channels":
            await self._edit_owner_screen(chat_id, message_id, self._owner_channels_text(), self._owner_channels_keyboard())
            return
        if data == "channel:list":
            await self._edit_owner_screen(chat_id, message_id, self._owner_channels_text(), self._owner_channels_keyboard())
            return
        if data == "channel:add":
            await self._edit_owner_screen(chat_id, message_id, "➕ Kanal qo‘shish\n\nKanal turini tanlang:", self._owner_channel_type_keyboard())
            return
        if data.startswith("channel:type:"):
            channel_type = data.split(":", 2)[2]
            if channel_type == "private":
                self._set_owner_session(user_id, "channel_add_private_forward")
                text = "🔐 Private/so‘rovli kanal\n\nKanal yoki guruhdan bitta xabarni shu chatga forward qiling. Bot u orqali chatni aniqlaydi."
            elif channel_type == "url":
                self._set_owner_session(user_id, "channel_add_url")
                text = "🌐 Oddiy URL kanal\n\nKanal yoki sahifa havolasini yuboring."
            else:
                state = "channel_add_main" if channel_type == "main" else ("channel_add_required" if channel_type == "required" else "channel_add_public")
                self._set_owner_session(user_id, state)
                label = {"main": "asosiy", "required": "majburiy obuna", "public": "ommaviy"}.get(channel_type, channel_type)
                text = f"📢 {label.title()} kanal\n\nKanal username’i yoki chat ID sini yuboring. Bot kanalga administrator qilib qo‘shilgan bo‘lishi kerak."
            await self._edit_owner_screen(chat_id, message_id, text, self._owner_channels_keyboard())
            return
        if data == "channel:delete":
            self._set_owner_session(user_id, "channel_delete")
            await self._edit_owner_screen(chat_id, message_id, "🗑 Kanalni o‘chirish\n\nO‘chiriladigan kanal chat ID sini yuboring:", self._owner_channels_keyboard())
            return
        if data == "owner:broadcast":
            await self._edit_owner_screen(chat_id, message_id, "✉️ Xabar yuborish\n\nKimga yuborishni tanlang:", self._owner_broadcast_keyboard())
            return
        if data == "owner:media":
            await self._edit_owner_screen(chat_id, message_id, self._owner_media_text(), self._owner_media_keyboard())
            return
        if data.startswith("owner:media:set:"):
            slot = data.rsplit(":", 1)[-1]
            if slot not in MEDIA_SLOTS:
                return
            label = MEDIA_SLOTS[slot][2]
            expected = "rasm" if MEDIA_SLOTS[slot][3] == "photo" else "video"
            self._set_owner_session(user_id, "media_upload", {"slot": slot})
            await self._edit_owner_screen(chat_id, message_id, f"🖼 {label}\n\n{expected.title()} yuboring.\n\nBekor qilish: /cancel", self._owner_media_keyboard())
            return
        if data.startswith("owner:media:remove:"):
            slot = data.rsplit(":", 1)[-1]
            config = MEDIA_SLOTS.get(slot)
            if config:
                self._delete_setting(config[0])
                self._delete_setting(config[1])
            await self._edit_owner_screen(chat_id, message_id, f"✅ {config[2] if config else 'Media'} o‘chirildi.", self._owner_media_keyboard())
            return
        if data == "broadcast:one":
            self._set_owner_session(user_id, "broadcast_one_id")
            await self._edit_owner_screen(chat_id, message_id, "👤 Bitta userga\n\nTelegram user ID sini yuboring:", self._owner_broadcast_keyboard())
            return
        if data in {"broadcast:all", "broadcast:vip", "broadcast:normal"}:
            target = data.split(":", 1)[1]
            target_label = {"all": "Barcha userlar", "vip": "VIP userlar", "normal": "Oddiy userlar"}[target]
            await self._edit_owner_screen(chat_id, message_id, f"✉️ {target_label}\n\nYuborish usulini tanlang:", self._owner_broadcast_type_keyboard(target))
            return
        if data == "broadcast:channels":
            self._set_owner_session(user_id, "broadcast_channel_select", {"selected": []})
            await self._edit_owner_screen(chat_id, message_id, "📢 Kanal tanlang:\n\nXabar yuboriladigan kanallarni belgilang:", self._owner_broadcast_channel_select_keyboard([]))
            return
        if data.startswith("broadcast:toggle:"):
            channel_id = data.split(":", 2)[2]
            session = self._owner_session(user_id) or {}
            session_data = session.get("data") if isinstance(session.get("data"), dict) else {}
            selected = [str(item) for item in session_data.get("selected", [])]
            if channel_id in selected:
                selected.remove(channel_id)
            else:
                selected.append(channel_id)
            self._set_owner_session(user_id, "broadcast_channel_select", {"selected": selected})
            await self._edit_owner_screen(chat_id, message_id, "📢 Kanal tanlang:\n\nXabar yuboriladigan kanallarni belgilang:", self._owner_broadcast_channel_select_keyboard(selected))
            return
        if data == "broadcast:send_selected":
            session = self._owner_session(user_id) or {}
            session_data = session.get("data") if isinstance(session.get("data"), dict) else {}
            selected = [str(item) for item in session_data.get("selected", [])]
            if not selected:
                await self.telegram.answer_callback_query(callback_id, "Avval kamida bitta kanal tanlang.", True)
                return
            await self._edit_owner_screen(chat_id, message_id, "📢 Tanlangan kanallarga\n\nYuborish usulini tanlang:", self._owner_broadcast_type_keyboard("channels", selected))
            return
        if data.startswith("broadcast:type:"):
            parts = data.split(":", 3)
            broadcast_type = parts[2]
            target = parts[3]
            extra = {}
            if target.startswith("channels|"):
                target, raw_ids = target.split("|", 1)
                extra["chat_ids"] = [item for item in raw_ids.split(",") if item]
            if target == "one":
                session = self._owner_session(user_id) or {}
                session_data = session.get("data") if isinstance(session.get("data"), dict) else {}
                extra["user_id"] = session_data.get("user_id")
            if broadcast_type == "text":
                self._set_owner_session(user_id, "broadcast_text", {"target": target, **extra})
                await self._edit_owner_screen(chat_id, message_id, "✍️ Yuboriladigan matnni yuboring.\n\nBekor qilish: /cancel", self._owner_broadcast_type_keyboard(target, extra.get("chat_ids", [])))
            else:
                self._set_owner_session(user_id, "broadcast_forward", {"target": target, **extra})
                await self._edit_owner_screen(chat_id, message_id, "↗️ Forward qilinadigan xabarni shu chatga yuboring yoki forward qiling.\n\nBekor qilish: /cancel", self._owner_broadcast_type_keyboard(target, extra.get("chat_ids", [])))
            return
        if data == "menu:home":
            await self._send_start_screen(chat_id, edit_message_id=message_id, user=sender)
            return
        if data == "menu:commands":
            await self._render_media_or_text(chat_id, COMMANDS_PAGE_1_TEXT, self._commands_page_1_keyboard(), "commands", message_id)
            return
        if data == "commands:next":
            await self._render_media_or_text(chat_id, COMMANDS_PAGE_2_TEXT, self._commands_page_2_keyboard(), "commands", message_id)
            return
        if data == "commands:back":
            await self._render_media_or_text(chat_id, COMMANDS_PAGE_1_TEXT, self._commands_page_1_keyboard(), "commands", message_id)
            return
        if data == "menu:guide":
            await self._render_media_or_text(chat_id, self._guide_caption(GUIDE_CONNECT_CAPTION), self._guide_keyboard(), "connect_guide", message_id)
            return
        if data == "guide:home":
            await self._render_media_or_text(chat_id, self._guide_caption(GUIDE_CONNECT_CAPTION), self._guide_keyboard(), "connect_guide", message_id)
            return
        if data == "guide:usage":
            await self._render_media_or_text(chat_id, self._guide_caption(GUIDE_USAGE_CAPTION), self._guide_usage_keyboard(), "usage_guide", message_id)
            return
        if data == "menu:profile":
            await self._render_media_or_text(chat_id, self._profile_text(user_id), self._profile_keyboard(), "start", message_id)
            return
        if data == "profile:home":
            await self._render_media_or_text(chat_id, self._profile_text(user_id), self._profile_keyboard(), "start", message_id)
            return
        if data == "profile:vip":
            await self._render_media_or_text(chat_id, VIP_FEATURES_TEXT, self._vip_features_keyboard(), "start", message_id)
            return
        if data == "profile:topup":
            await self._render_media_or_text(chat_id, "💳 Balansni to‘ldirish\n\nTo‘lov usulini tanlang:", self._topup_keyboard(), "start", message_id)
            return
        if data == "menu:settings":
            await self._render_media_or_text(chat_id, self._settings_text(user_id), self._settings_keyboard(user_id), "start", message_id)
            return
        if data in {"settings:edit", "settings:delete", "settings:deletions"}:
            kind = "delete" if data != "settings:edit" else "edit"
            await self._render_media_or_text(chat_id, self._settings_feature_text(kind, user_id), self._settings_feature_keyboard(kind, user_id), "start", message_id)
            return
        if data.startswith(("settings:edit:", "settings:delete:")):
            parts = data.split(":")
            kind = parts[1]
            field = parts[2] if len(parts) > 2 else ""
            if len(parts) == 3 and field in {"dest", "type"}:
                await self._render_media_or_text(chat_id, self._settings_feature_text(kind, user_id), self._settings_option_keyboard(kind, field), "start", message_id)
                return
            if len(parts) == 3 and field in {"toggle", "time"}:
                key = f"{kind}_notify_enabled" if field == "toggle" else f"{kind}_notify_timestamp"
                default = "0" if (kind == "edit" or field == "time") else "1"
                current = self._user_setting(user_id, key, default) == "1"
                enabled = not current
                self._set_user_setting(user_id, key, "1" if enabled else "0")
                if field == "toggle":
                    label = "Tahrirlangan xabarlarni bildirish" if kind == "edit" else "O‘chirilgan xabarlarni bildirish"
                else:
                    label = "Yuborilgan va tahrirlangan vaqtni ko‘rsatish" if kind == "edit" else "Yuborilgan va o‘chirilgan vaqtni ko‘rsatish"
                await self.telegram.answer_callback_query(callback_id, f"{label} {'yoqildi 🔔' if enabled else 'o‘chirildi 🔕'}!", True)
                await self._render_media_or_text(chat_id, self._settings_feature_text(kind, user_id), self._settings_feature_keyboard(kind, user_id), "start", message_id)
                return
            if len(parts) == 4:
                value = parts[3]
                if field == "dest" and value in {"chat", "bot"}:
                    self._set_user_setting(user_id, f"{kind}_notify_destination", value)
                elif field == "type" and value in {"notification", "copy"}:
                    self._set_user_setting(user_id, f"{kind}_notify_type", value)
                else:
                    return
                await self._render_media_or_text(chat_id, self._settings_feature_text(kind, user_id), self._settings_feature_keyboard(kind, user_id), "start", message_id)
                return
        if data == "settings:commands":
            await self._render_media_or_text(chat_id, "📗 Buyruqlar ruxsati\n\nQaysi buyruqni kim ishlatishini Telegram Business sozlamalaridan belgilaysiz.", self._settings_back_keyboard(), "start", message_id)
            return
        toggle_settings = {
            "settings:apk": ("settings_apk_delete_enabled", "1", "APK o‘chirish"),
            "settings:typing": ("settings_typing_enabled", "1", "Yozmoqda"),
            "settings:read": ("settings_read_enabled", "1", "Avto javob berganda xabarni o‘qish"),
            "settings:currency": ("settings_currency_enabled", "1", "Valyuta miqdorini hisoblash"),
        }
        if data in toggle_settings:
            key, default, label = toggle_settings[data]
            current = self._user_setting(user_id, key, default) == "1"
            enabled = not current
            self._set_user_setting(user_id, key, "1" if enabled else "0")
            await self.telegram.answer_callback_query(callback_id, f"{label} {'yoqildi 🔔' if enabled else 'o‘chirildi 🔕'}!", True)
            await self._render_media_or_text(chat_id, self._settings_text(user_id), self._settings_keyboard(user_id), "start", message_id)
            return
        if data in {"settings:pro", "settings:business"}:
            title = "🧙 Pro funksiyalar" if data == "settings:pro" else "😎 Biznes funksiyalar"
            await self._render_media_or_text(chat_id, f"{title}\n\nVIP 💎 imkoniyatlari uchun quyidagi tugmani bosing.", {"inline_keyboard": [[{"text": "VIP 💎", "callback_data": "profile:vip"}], [{"text": "🔙 Sozlamalar", "callback_data": "menu:settings"}]]}, "start", message_id)
            return
        if data == "menu:auto_replies":
            await self._render_media_or_text(chat_id, self._auto_replies_text(user_id), self._auto_replies_keyboard(user_id), "start", message_id)
            return
        if data == "auto:add":
            self._set_user_session(user_id, "auto_add_trigger")
            await self._render_media_or_text(chat_id, "➕ Avto javob qo‘shish\n\nTrigger so‘z yoki iborani yuboring. Bekor qilish: /cancel", self._auto_reply_back_keyboard(), "start", message_id)
            return
        if data == "auto:permissions":
            await self._render_media_or_text(chat_id, self._auto_permissions_text(user_id), self._auto_permissions_keyboard(), "start", message_id)
            return
        if data.startswith("auto:view:"):
            record_id = self._parse_record_id(data)
            record = self._get_auto_reply(user_id, record_id) if record_id is not None else None
            if record:
                await self._render_media_or_text(chat_id, self._auto_reply_detail_text(record), self._auto_reply_detail_keyboard(record), "start", message_id)
            else:
                await self.telegram.answer_callback_query(callback_id, "Avto javob topilmadi.", True)
            return
        if data.startswith("auto:delete:"):
            record_id = self._parse_record_id(data)
            record = self._get_auto_reply(user_id, record_id) if record_id is not None else None
            if record:
                await self._render_media_or_text(chat_id, "🗑 Avto javobni o‘chirishni tasdiqlaysizmi?\n\n" + str(record.get("trigger") or ""), self._auto_delete_confirm_keyboard(record_id), "start", message_id)
            else:
                await self.telegram.answer_callback_query(callback_id, "Avto javob topilmadi.", True)
            return
        if data.startswith("auto:delete_confirm:"):
            record_id = self._parse_record_id(data)
            deleted = self._delete_auto_reply(user_id, record_id) if isinstance(user_id, int) and record_id is not None else False
            notice = "✅ Avto javob o‘chirildi." if deleted else "Avto javob topilmadi."
            await self.telegram.answer_callback_query(callback_id, notice, True)
            await self._render_media_or_text(chat_id, self._auto_replies_text(user_id), self._auto_replies_keyboard(user_id), "start", message_id)
            return
        if data.startswith("auto:option:"):
            parts = data.split(":")
            record_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            option = parts[3] if len(parts) > 3 else ""
            record = self._get_auto_reply(user_id, record_id)
            if record and option in {"enabled", "reply_in_message", "reply_to_owner"} and isinstance(user_id, int):
                enabled = not bool(record.get(option, False))
                setter = getattr(self.store, "set_auto_reply_option", None)
                changed = bool(callable(setter) and setter(user_id, record_id, option, enabled))
                await self.telegram.answer_callback_query(callback_id, ("✅ Holat yangilandi." if changed else "Holatni yangilab bo‘lmadi."), True)
                record = self._get_auto_reply(user_id, record_id) or record
                await self._render_media_or_text(chat_id, self._auto_reply_detail_text(record), self._auto_reply_detail_keyboard(record), "start", message_id)
            return
        if data.startswith("auto:edit:"):
            record_id = self._parse_record_id(data)
            record = self._get_auto_reply(user_id, record_id) if record_id is not None else None
            if not record:
                await self.telegram.answer_callback_query(callback_id, "Avto javob topilmadi.", True)
                return
            self._set_user_session(user_id, "auto_edit", {"record_id": record_id})
            await self._render_media_or_text(chat_id, "✏️ Avto javobni tahrirlash\n\nYangi trigger va javobni quyidagi ko‘rinishda yuboring:\ntrigger | javob\n\nBekor qilish: /cancel", self._auto_reply_back_keyboard(), "start", message_id)
            return
        if data.startswith("auto:toggle:"):
            command = data.rsplit(":", 1)[-1]
            if command not in AUTO_REPLY_COMMANDS:
                return
            key = f"auto_reply_{command}_permission"
            current = self._user_setting(user_id, key, "all")
            enabled_for_all = current != "all"
            self._set_user_setting(user_id, key, "all" if enabled_for_all else "none")
            status = "Hamma" if enabled_for_all else "Hech kim"
            await self.telegram.answer_callback_query(callback_id, f".{command} buyrug‘i: {status} ✅", True)
            await self._render_media_or_text(chat_id, self._auto_replies_text(user_id), self._auto_replies_keyboard(user_id), "start", message_id)
            return
        if data == "menu:about":
            await self._render_media_or_text(chat_id, BOT_ABOUT_TEXT, self._about_keyboard(), "start", message_id)
            return
        if data == "premium:status":
            text, markup = self._premium_panel_content(user_id)
            await self._render_media_or_text(chat_id, text, markup, "start", message_id)
            return
        if data == "premium:buy":
            await self._send_subscription_offer(chat_id, user_id, None, edit_message_id=message_id)
            return
        if data == "premium:role":
            await self._render_media_or_text(chat_id, "Shaxsiy AI rolingizni o‘zgartirish uchun /rol Sizning uslubingiz... buyrug‘ini yuboring.", self._premium_role_keyboard(), "start", message_id)
            return
        if data in {"admin:home", "admin:stats", "admin:role", "admin:pause", "admin:pause:toggle"}:
            if data == "admin:stats":
                text = self._admin_stats_text()
                markup = self._admin_back_keyboard()
            elif data == "admin:role":
                if is_owner:
                    role = self.store.get_role("")
                    text = "🧠 AI roli\n\n" + (role or "Qo‘shimcha rol berilmagan; AI oddiy javob rejimida ishlaydi.")
                else:
                    getter = getattr(self.store, "get_user_role", None)
                    role = getter(user_id, "") if callable(getter) else ""
                    text = "🧠 Shaxsiy AI roli\n\n" + (role or "Qo‘shimcha shaxsiy rol berilmagan; AI oddiy javob rejimida ishlaydi.")
                markup = {"inline_keyboard": [[{"text": "♻️ Rolni tozalash", "callback_data": "admin:role:reset"}], [{"text": "🔙 Admin panel", "callback_data": "admin:home"}]]}
            elif data in {"admin:pause", "admin:pause:toggle"}:
                if data == "admin:pause:toggle":
                    self._set_manual_pause_enabled(not self._manual_pause_enabled(user_id), user_id)
                enabled = self._manual_pause_enabled(user_id)
                state = "YOQILGAN" if enabled else "O‘CHIRILGAN"
                text = f"⏱ Manual pause: {state}\n\nYoqilganda egasi mijozga qo‘lda yozganidan keyin shu chatda AI javobi 30 daqiqaga to‘xtaydi. O‘chirilganda bot 30 daqiqalik qoida bo‘yicha pauza qilmaydi."
                markup = self._admin_pause_keyboard(enabled)
            else:
                text = self._admin_panel_text()
                markup = self._admin_panel_keyboard(include_statistics=is_owner, include_main_menu=True, user_id=user_id, include_owner_tools=is_owner)
            try:
                await self.telegram.edit_message_text(chat_id, message_id, text, markup)
            except TelegramApiError:
                await self._send_chunks(chat_id, text, None, None, markup)
            return
        if data == "admin:role:reset":
            if is_owner:
                self.store.clear_role()
                reset_text = "✅ AI roli standart holatga qaytarildi."
            else:
                clearer = getattr(self.store, "clear_user_role", None)
                if callable(clearer):
                    clearer(user_id)
                reset_text = "✅ Shaxsiy AI roli standart holatga qaytarildi."
            try:
                await self.telegram.edit_message_text(chat_id, message_id, reset_text, self._admin_back_keyboard())
            except TelegramApiError:
                await self._send_chunks(chat_id, reset_text, None, None, self._admin_back_keyboard())

    def _has_premium(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        method = getattr(self.store, "has_premium", None)
        return bool(callable(method) and method(user_id))

    def _premium_until(self, user_id: int | None) -> float | None:
        if user_id is None:
            return None
        method = getattr(self.store, "premium_until", None)
        return method(user_id) if callable(method) else None

    async def _send_subscription_offer(self, chat_id: int, user_id: int | None, reply_to: int | None, edit_message_id: int | None = None) -> None:
        if user_id is not None and self._has_premium(user_id):
            text, markup = self._premium_panel_content(user_id)
            if edit_message_id is not None:
                await self._render_media_or_text(chat_id, text, markup, "start", edit_message_id)
            else:
                await self._send_chunks(chat_id, text, None, reply_to, markup)
            return
        try:
            link = await self.telegram.create_invoice_link(
                "VIP AI — 1 oy",
                "AI chat, shaxsiy rol va VIP funksiyalar. Obuna 30 kun amal qiladi.",
                STAR_SUBSCRIPTION_PAYLOAD,
                STAR_SUBSCRIPTION_AMOUNT,
                STAR_SUBSCRIPTION_PERIOD_SECONDS,
            )
            markup = {"inline_keyboard": [[{"text": "⭐ 100 Stars — obuna bo‘lish", "url": link}]]}
            text = "VIP funksiyalarni ochish uchun oyiga 100 Telegram Stars to‘lang. To‘lov muvaffaqiyatli tasdiqlangach, VIP access 30 kunga avtomatik ochiladi."
            if edit_message_id is not None:
                await self._render_media_or_text(chat_id, text, markup, "start", edit_message_id)
            else:
                await self._send_chunks(chat_id, text, None, reply_to, markup)
        except (TelegramApiError, ProviderError) as exc:
            LOGGER.error("Stars invoice yaratishda xato: %s", exc)
            await self._send_chunks(chat_id, "Hozircha to‘lov havolasini yaratib bo‘lmadi. Keyinroq yana urinib ko‘ring.", None, reply_to)

    def _premium_panel_content(self, user_id: int) -> tuple[str, dict[str, Any]]:
        until = self._premium_until(user_id)
        active = self._has_premium(user_id)
        if active and until:
            remaining_days = max(1, int((until - time.time()) / 86400))
            text = f"⭐ VIP faol. Qolgan muddat: taxminan {remaining_days} kun.\n\nShaxsiy AI rolingizni /rol orqali sozlashingiz mumkin."
        else:
            text = "⭐ VIP faol emas. Oylik 100 Stars obunasi bilan AI chat, shaxsiy rol va boshqa VIP funksiyalarni oching."
        return text, self._premium_keyboard(active)

    async def _send_premium_panel(self, chat_id: int, user_id: int | None, reply_to: int | None) -> None:
        if user_id is None:
            await self._send_chunks(chat_id, "VIP panelni ochib bo‘lmadi.", None, reply_to)
            return
        text, markup = self._premium_panel_content(user_id)
        await self._send_chunks(chat_id, text, None, reply_to, markup)

    async def _edit_premium_panel(self, chat_id: int, user_id: int | None, message_id: int) -> None:
        if user_id is None:
            return
        text, markup = self._premium_panel_content(user_id)
        try:
            await self.telegram.edit_message_text(chat_id, message_id, text, markup)
        except TelegramApiError:
            await self._send_chunks(chat_id, text, None, None, markup)

    def _profile_text(self, user_id: int | None) -> str:
        is_owner = user_id == OWNER_ADMIN_ID
        active = is_owner or self._has_premium(user_id)
        if is_owner:
            plan = "Owner ∞"
            expiry = "∞"
            limits = "📩 Avto javoblar: ∞\n🤖 AI avto javob (kunlik): ∞\n🧠 «.ai» savol (kunlik): ∞\n🖼 «.img» / «.rasm» (kunlik): ∞"
        elif active:
            plan = "VIP 💎"
            until = self._premium_until(user_id)
            expiry = time.strftime("%Y-%m-%d", time.localtime(until)) if until else "—"
            limits = "📩 Avto javoblar: 100 ta\n🤖 AI avto javob (kunlik): 500 ta\n🧠 «.ai» savol (kunlik): 100 ta\n🖼 «.img» / «.rasm» (kunlik): 5 ta"
        else:
            plan = "Bepul"
            expiry = "—"
            limits = "💬 Avto javoblar: 0/5\n🤖 AI avto javob (bugun): 0/500\n🧠 AI javob limiti: 25"
        return f"👤 Profil:\n\n👑 Tarifingiz: {plan}\n⏰ Tarif tugashi: {expiry}\n\n{limits}"

    @staticmethod
    def _profile_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "VIP 💎", "callback_data": "profile:vip"}],
            [{"text": "💳 Balansni to‘ldirish", "callback_data": "profile:topup"}],
            [{"text": "🔙 Orqaga", "callback_data": "menu:home"}],
        ]}

    @staticmethod
    def _vip_features_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "⭐ 100 Stars — 1 oy VIP", "callback_data": "premium:buy"}],
            [{"text": "🔙 Profilim", "callback_data": "profile:home"}],
        ]}

    @staticmethod
    def _topup_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "⭐ Avto to‘lov (stars)", "callback_data": "premium:buy"}],
            [{"text": "🔙 Orqaga", "callback_data": "profile:home"}],
        ]}

    def _settings_text(self, user_id: int | None) -> str:
        return """@InfoUchihaBot sozlamalari ⚙️

🤷‍♂️ Bepul sozlamalar:

Qisqa qo‘llanma (ochish uchun bosing):

💬 Avto javoblar — suhbatdosh yozgan so‘zga bot o‘zi javob beradi.
✏️ Tahrirlanish — suhbatdosh xabarini tahrirlasa bildiriladi.
🗑 O‘chirishlar — suhbatdosh xabarini o‘chirsa bildiriladi.
🤖 APK o‘chirish — chatga tashlangan .apk fayl avtomatik o‘chadi.
••• Yozmoqda — javobdan oldin «yozmoqda...» ko‘rinadi.
✉️ Xabarni o‘qish — bot javob berganda xabar «o‘qilgan» bo‘ladi.
💱 Valyuta hisoblash — «100 USD» deb yozilsa kursini hisoblab beradi.
🟢 Buyruqlar ruxsati — qaysi buyruqni kim ishlatishini belgilaysiz."""

    def _user_setting(self, user_id: int, key: str, default: str) -> str:
        getter = getattr(self.store, "get_user_setting", None)
        if callable(getter):
            try:
                return str(getter(user_id, key, default) or default)
            except Exception as exc:
                LOGGER.warning("User setting o‘qilmadi user=%s key=%s: %s", user_id, key, exc)
        return default

    def _set_user_setting(self, user_id: int, key: str, value: str) -> None:
        setter = getattr(self.store, "set_user_setting", None)
        if callable(setter):
            try:
                setter(user_id, key, value)
            except Exception as exc:
                LOGGER.warning("User setting yozilmadi user=%s key=%s: %s", user_id, key, exc)

    def _vip_settings_allowed(self, user_id: int | None) -> bool:
        return isinstance(user_id, int) and (user_id == OWNER_ADMIN_ID or self._has_premium(user_id))

    def _settings_keyboard(self, user_id: int | None = None) -> dict[str, Any]:
        uid = user_id if isinstance(user_id, int) else 0
        edit_state = self._user_setting(uid, "edit_notify_enabled", "0") == "1" if uid else False
        delete_state = self._user_setting(uid, "delete_notify_enabled", "1") == "1" if uid else True
        return {"inline_keyboard": [
            [{"text": "🤝 Chatbotni sozlash", "url": "tg://settings/edit"}],
            [{"text": f"✏️ Tahrirlash: {'on' if edit_state else 'off'}", "callback_data": "settings:edit"}, {"text": f"🗑 O‘chirishlar: {'on' if delete_state else 'off'}", "callback_data": "settings:delete"}],
            [{"text": f"🤖 APK o‘chirish: {'on' if self._user_setting(uid, 'settings_apk_delete_enabled', '1') == '1' else 'off'}", "callback_data": "settings:apk"}, {"text": f"••• Yozmoqda: {'on' if self._user_setting(uid, 'settings_typing_enabled', '1') == '1' else 'off'}", "callback_data": "settings:typing"}],
            [{"text": f"✉️ Avto javob berganda xabarni o‘qish: {'on' if self._user_setting(uid, 'settings_read_enabled', '1') == '1' else 'off'}", "callback_data": "settings:read"}],
            [{"text": f"💱 Valyuta miqdorini hisoblash: {'on' if self._user_setting(uid, 'settings_currency_enabled', '1') == '1' else 'off'}", "callback_data": "settings:currency"}],
            [{"text": "📗 Buyruqlarni kim ishlata oladi", "callback_data": "settings:commands"}],
            [{"text": "VIP 💎 obuna", "callback_data": "profile:vip"}],
            [{"text": "🔙 Orqaga", "callback_data": "menu:home"}],
        ]}

    @staticmethod
    def _settings_back_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "🔙 Sozlamalar", "callback_data": "menu:settings"}], [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}]]}

    @staticmethod
    def _settings_option_keyboard(kind: str, field: str) -> dict[str, Any]:
        prefix = f"settings:{kind}:{field}"
        if field == "dest":
            return {"inline_keyboard": [[
                {"text": "🗣 Suhbatdoshga", "callback_data": f"{prefix}:chat"},
                {"text": "🤖 Botga", "callback_data": f"{prefix}:bot"},
            ], [{"text": "🔙 Orqaga", "callback_data": f"settings:{kind}"}]]}
        if field == "type":
            return {"inline_keyboard": [[
                {"text": "📩 Bildirishnoma", "callback_data": f"{prefix}:notification"},
                {"text": "📋 Xabar nusxasi", "callback_data": f"{prefix}:copy"},
            ], [{"text": "🔙 Orqaga", "callback_data": f"settings:{kind}"}]]}
        return {"inline_keyboard": [[{"text": "🔙 Orqaga", "callback_data": f"settings:{kind}"}]]}

    def _settings_feature_text(self, kind: str, user_id: int) -> str:
        is_edit = kind == "edit"
        enabled = self._user_setting(user_id, f"{kind}_notify_enabled", "0" if is_edit else "1") == "1"
        destination = self._user_setting(user_id, f"{kind}_notify_destination", "chat" if is_edit else "bot")
        message_type = self._user_setting(user_id, f"{kind}_notify_type", "notification")
        show_time = self._user_setting(user_id, f"{kind}_notify_timestamp", "0") == "1"
        if is_edit:
            title = "👤 Suhbatdoshingiz o‘zini xabarini tahrirlaganda unga yuboriladigan Tahrirlangan xabar ko‘rinishi 📬"
            action = "✏️ Tahrirlangan xabarni yuborish"
            example = "📩 Suhbatdosh quyidagi xabarini tahrirladi:\n\n💬 Salom, narxi qancha?\n\n✏️ Salom, narxi qanchaligini ayta olasizmi?"
        else:
            title = "👤 Suhbatdoshingiz o‘zini xabarini o‘chirganda botga yuboriladigan O‘chirilgan xabar ko‘rinishi 📬"
            action = "🗑 O‘chirilgan xabarni yuborish"
            example = "🗑 Suhbatdosh quyidagi xabarini o‘chirdi:\n\n💬 Salom, narxi qancha?"
        destination_label = "Suhbatdoshga" if destination == "chat" else "Botga"
        type_label = "Bildirishnoma" if message_type == "notification" else ("Tahrirlangan xabar nusxasi" if is_edit else "O‘chirilgan xabar nusxasi")
        type_target = "suhbatdoshga" if destination == "chat" else "botga"
        time_label = "on" if show_time else "off"
        return f"{title}\n\n{example}\n\n{action}: {'on' if enabled else 'off'}\n🤷‍♂️ Qayerga yuborilsin: {destination_label}\n💬 Xabar turi {type_target}: {type_label}\n⏰ Yuborilgan va {'tahrirlangan' if is_edit else 'o‘chirilgan'} vaqtni ko‘rsatish: {time_label}"

    def _settings_feature_keyboard(self, kind: str, user_id: int) -> dict[str, Any]:
        is_edit = kind == "edit"
        enabled = self._user_setting(user_id, f"{kind}_notify_enabled", "0" if is_edit else "1") == "1"
        destination = self._user_setting(user_id, f"{kind}_notify_destination", "chat" if is_edit else "bot")
        message_type = self._user_setting(user_id, f"{kind}_notify_type", "notification")
        show_time = self._user_setting(user_id, f"{kind}_notify_timestamp", "0") == "1"
        action = "✏️ Tahrirlangan xabarni yuborish" if is_edit else "🗑 O‘chirilgan xabarni yuborish"
        dest_label = "Suhbatdoshga" if destination == "chat" else "Botga"
        type_label = "Bildirishnoma" if message_type == "notification" else ("Tahrirlangan xabar nusxasi" if is_edit else "O‘chirilgan xabar nusxasi")
        type_target = "suhbatdoshga" if destination == "chat" else "botga"
        time_label = "on" if show_time else "off"
        return {"inline_keyboard": [
            [{"text": f"{action}: {'on' if enabled else 'off'}", "callback_data": f"settings:{kind}:toggle"}],
            [{"text": f"🤷‍♂️ Qayerga yuborilsin: {dest_label}", "callback_data": f"settings:{kind}:dest"}],
            [{"text": f"💬 Xabar turi {type_target}: {type_label}", "callback_data": f"settings:{kind}:type"}],
            [{"text": f"⏰ Yuborilgan va {'tahrirlangan' if is_edit else 'o‘chirilgan'} vaqtni ko‘rsatish: {time_label}", "callback_data": f"settings:{kind}:time"}],
            [{"text": "🔙 Sozlamalar", "callback_data": "menu:settings"}],
        ]}

    def _get_setting(self, key: str, default: str = "") -> str:
        getter = getattr(self.store, "get_setting", None)
        if not callable(getter):
            return default
        try:
            return str(getter(key, default) or default)
        except Exception as exc:
            LOGGER.warning("Setting o‘qilmadi key=%s: %s", key, exc)
            return default

    def _set_setting(self, key: str, value: str) -> None:
        setter = getattr(self.store, "set_setting", None)
        if callable(setter):
            setter(key, value)

    def _delete_setting(self, key: str) -> None:
        deleter = getattr(self.store, "delete_setting", None)
        if callable(deleter):
            deleter(key)

    def _media_config(self, slot: str) -> tuple[str, str] | None:
        config = MEDIA_SLOTS.get(slot)
        if not config:
            return None
        file_key, type_key, _label, default_type = config
        file_id = self._get_setting(file_key).strip()
        if not file_id:
            return None
        return file_id, self._get_setting(type_key, default_type).strip() or default_type

    def _main_channel_username(self) -> str:
        getter = getattr(self.store, "list_channels", None)
        channels = getter() if callable(getter) else []
        for channel in channels:
            if isinstance(channel, dict) and channel.get("is_main") is True:
                username = str(channel.get("username") or "").strip()
                if username:
                    return username if username.startswith("@") else f"@{username}"
        return "—"

    def _guide_caption(self, base: str) -> str:
        return f"{base}\n\n📣 Kanalimiz: {self._main_channel_username()}"

    def _main_menu_keyboard(self, premium_active: bool = False) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "📚 Buyruqlar", "callback_data": "menu:commands"}, {"text": "🦉 Qo‘llanma", "callback_data": "menu:guide"}],
            [{"text": "👤 Profilim", "callback_data": "menu:profile"}, {"text": "⚙️ Sozlamalar", "callback_data": "menu:settings"}],
            [{"text": "💬 Avto javoblar ro‘yxati", "callback_data": "menu:auto_replies"}],
        ]}

    @staticmethod
    def _commands_page_1_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "➡️ Davomi", "callback_data": "commands:next"}],
            [{"text": "🔙 Orqaga", "callback_data": "menu:home"}],
        ]}

    @staticmethod
    def _commands_page_2_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "⬅️ Avvalgi sahifa", "callback_data": "commands:back"}],
            [{"text": "🔙 Orqaga", "callback_data": "menu:home"}],
        ]}

    @staticmethod
    def _guide_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "🦉 Foydalanish qo‘llanmasi", "callback_data": "guide:usage"}],
            [{"text": "🔙 Orqaga", "callback_data": "menu:home"}],
        ]}

    @staticmethod
    def _guide_usage_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "🔙 Qo‘llanma", "callback_data": "guide:home"}],
        ]}

    @staticmethod
    def _media_caption(text: str) -> str:
        # Photo/video caption Telegramda 1024 belgidan oshmasligi kerak.
        text = str(text)
        return text if len(text) <= 1024 else text[:1021].rstrip() + "…"

    async def _render_media_or_text(
        self,
        chat_id: int,
        text: str,
        markup: dict[str, Any],
        slot: str | None = None,
        edit_message_id: int | None = None,
    ) -> None:
        media = self._media_config(slot) if slot else None
        if media:
            file_id, media_type = media
            if edit_message_id is not None:
                try:
                    await self.telegram.edit_message_media(chat_id, edit_message_id, media_type, file_id, self._media_caption(text), markup)
                    return
                except (TelegramApiError, AttributeError) as exc:
                    LOGGER.warning("Media xabarini tahrirlashda xato slot=%s: %s", slot, exc)
                    try:
                        await self.telegram.edit_message_caption(chat_id, edit_message_id, text, markup)
                        return
                    except (TelegramApiError, AttributeError) as caption_exc:
                        LOGGER.warning("Media caption/keyboard fallback ham ishlamadi slot=%s: %s", slot, caption_exc)
                        try:
                            await self.telegram.edit_message_text(chat_id, edit_message_id, text, markup)
                            return
                        except (TelegramApiError, AttributeError) as text_exc:
                            LOGGER.warning("Media text fallback ham ishlamadi slot=%s: %s", slot, text_exc)
            try:
                if media_type == "video":
                    await self.telegram.send_video(chat_id, file_id, self._media_caption(text), reply_markup=markup)
                else:
                    await self.telegram.send_photo(chat_id, file_id, self._media_caption(text), reply_markup=markup)
                return
            except TelegramApiError as exc:
                LOGGER.warning("Konfiguratsiya qilingan media yuborilmadi slot=%s: %s", slot, exc)
        if edit_message_id is not None:
            try:
                await self.telegram.edit_message_text(chat_id, edit_message_id, text, markup)
                return
            except (TelegramApiError, AttributeError) as exc:
                try:
                    await self.telegram.edit_message_caption(chat_id, edit_message_id, text, markup)
                    return
                except (TelegramApiError, AttributeError) as caption_exc:
                    LOGGER.warning("Text/caption media fallback ishlamadi: %s; %s", exc, caption_exc)
        await self._send_chunks(chat_id, text, None, None, markup)

    @staticmethod
    def _display_name(user: dict[str, Any] | None) -> str:
        if isinstance(user, dict):
            first_name = " ".join(str(user.get("first_name") or "").split()).strip()
            last_name = " ".join(str(user.get("last_name") or "").split()).strip()
            name = " ".join(part for part in (first_name, last_name) if part)
            if name:
                return name[:64]
            username = " ".join(str(user.get("username") or "").split()).strip().lstrip("@")
            if username:
                return f"@{username}"[:64]
        return "do‘st"

    def _start_menu_text(self, user: dict[str, Any] | None = None) -> str:
        return START_MENU_TEXT_TEMPLATE.format(name=self._display_name(user))

    async def _send_start_screen(self, chat_id: int, reply_to: int | None = None, edit_message_id: int | None = None, user: dict[str, Any] | None = None) -> None:
        start_text = self._start_menu_text(user)
        media = self._media_config("start")
        if media:
            if edit_message_id is not None:
                try:
                    await self.telegram.edit_message_media(chat_id, edit_message_id, media[1], media[0], self._media_caption(start_text), self._main_menu_keyboard())
                    return
                except (TelegramApiError, AttributeError) as exc:
                    LOGGER.warning("Start media xabarini tahrirlashda xato: %s", exc)
                    try:
                        await self.telegram.edit_message_caption(chat_id, edit_message_id, start_text, self._main_menu_keyboard())
                        return
                    except (TelegramApiError, AttributeError) as caption_exc:
                        LOGGER.warning("Start media caption/keyboard fallback ham ishlamadi: %s", caption_exc)
                        try:
                            await self.telegram.edit_message_text(chat_id, edit_message_id, start_text, self._main_menu_keyboard())
                            return
                        except (TelegramApiError, AttributeError) as text_exc:
                            LOGGER.warning("Start media text fallback ham ishlamadi: %s", text_exc)
            try:
                await self.telegram.send_photo(chat_id, media[0], self._media_caption(start_text), reply_markup=self._main_menu_keyboard())
                return
            except TelegramApiError as exc:
                LOGGER.warning("Start rasmi yuborilmadi: %s", exc)
        if edit_message_id is not None:
            try:
                await self.telegram.edit_message_text(chat_id, edit_message_id, start_text, self._main_menu_keyboard())
                return
            except (TelegramApiError, AttributeError) as exc:
                try:
                    await self.telegram.edit_message_caption(chat_id, edit_message_id, start_text, self._main_menu_keyboard())
                    return
                except (TelegramApiError, AttributeError) as caption_exc:
                    LOGGER.warning("Start text/caption media fallback ishlamadi: %s; %s", exc, caption_exc)
        await self._send_chunks(chat_id, start_text, None, reply_to, self._main_menu_keyboard())

    def _auto_replies_text(self, user_id: int | None) -> str:
        lines = ["💬 Avto javoblar ro‘yxati", "", "⚙️ Buyruqlar ruxsati:"]
        for command in AUTO_REPLY_COMMANDS:
            status = "Hamma" if self._user_setting(user_id or 0, f"auto_reply_{command}_permission", "all") == "all" else "Hech kim"
            lines.append(f".{command} ni ishlatish: {status}")
        records = self._list_auto_replies(user_id)
        lines.extend(["", "📩 Shaxsiy avto javoblar:"])
        if records:
            for row in records[:20]:
                state = "on" if row.get("enabled", True) else "off"
                lines.append(f"• {row.get('trigger', '')} → {str(row.get('response', ''))[:80]} ({state})")
        else:
            lines.append("Hozircha avto javoblar qo‘shilmagan.")
        return "\n".join(lines)

    def _auto_replies_keyboard(self, user_id: int | None) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = [[{"text": "➕ Avto javob qo‘shish", "callback_data": "auto:add"}]]
        for row in self._list_auto_replies(user_id)[:20]:
            record_id = int(row.get("id", 0))
            trigger = str(row.get("trigger", ""))[:28]
            rows.append([{"text": f"📩 {trigger}", "callback_data": f"auto:view:{record_id}"}])
            rows.append([
                {"text": "✏️ Tahrirlash", "callback_data": f"auto:edit:{record_id}"},
                {"text": "🗑 O‘chirish", "callback_data": f"auto:delete:{record_id}"},
            ])
        rows.append([{"text": "⚙️ Buyruqlar ruxsati", "callback_data": "auto:permissions"}])
        rows.append([{"text": "🔙 Orqaga", "callback_data": "menu:home"}])
        return {"inline_keyboard": rows}

    def _auto_permissions_text(self, user_id: int | None) -> str:
        lines = ["⚙️ Buyruqlar ruxsati", "", "Har bir tugma bosilganda Hamma / Hech kim holati almashadi:"]
        for command in AUTO_REPLY_COMMANDS:
            status = "Hamma" if self._user_setting(user_id or 0, f"auto_reply_{command}_permission", "all") == "all" else "Hech kim"
            lines.append(f".{command}: {status}")
        return "\n".join(lines)

    def _auto_permissions_keyboard(self) -> dict[str, Any]:
        rows = [[{"text": f".{command}", "callback_data": f"auto:toggle:{command}"}] for command in AUTO_REPLY_COMMANDS]
        rows.extend([[{"text": "🔙 Avto javoblar ro‘yxati", "callback_data": "menu:auto_replies"}], [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}]])
        return {"inline_keyboard": rows}

    @staticmethod
    def _auto_reply_back_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "🔙 Avto javoblar ro‘yxati", "callback_data": "menu:auto_replies"}], [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}]]}

    @staticmethod
    def _auto_delete_confirm_keyboard(record_id: int) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "✅ Ha, o‘chirish", "callback_data": f"auto:delete_confirm:{record_id}"}, {"text": "❌ Yo‘q", "callback_data": f"auto:view:{record_id}"}], [{"text": "🔙 Ro‘yxat", "callback_data": "menu:auto_replies"}]]}

    @staticmethod
    def _auto_reply_detail_text(record: dict[str, object]) -> str:
        enabled = "on" if record.get("enabled", True) else "off"
        in_message = "on" if record.get("reply_in_message", False) else "off"
        to_owner = "on" if record.get("reply_to_owner", False) else "off"
        return (f"💬 Avto javob\n\nSuhbatdosh: barcha suhbatdoshlar\nTrigger: {record.get('trigger', '')}\n"
                f"Javob: {record.get('response', '')}\n\nAvto javob: {enabled}\n"
                f"Xabar ichida mos bo‘lsa: {in_message}\nO‘zimga javob bersin: {to_owner}")

    @staticmethod
    def _auto_reply_detail_keyboard(record: dict[str, object]) -> dict[str, Any]:
        record_id = int(record.get("id", 0))
        state = "on" if record.get("enabled", True) else "off"
        in_message = "on" if record.get("reply_in_message", False) else "off"
        to_owner = "on" if record.get("reply_to_owner", False) else "off"
        return {"inline_keyboard": [
            [{"text": f"Avto javob: {state}", "callback_data": f"auto:option:{record_id}:enabled"}],
            [{"text": f"Xabar ichida soz bo‘lsa: {in_message}", "callback_data": f"auto:option:{record_id}:reply_in_message"}],
            [{"text": f"O‘zimga javob bersin: {to_owner}", "callback_data": f"auto:option:{record_id}:reply_to_owner"}],
            [{"text": "✏️ Javobni tahrirlash", "callback_data": f"auto:edit:{record_id}"}],
            [{"text": "🗑 Avto javobni o‘chirish", "callback_data": f"auto:delete:{record_id}"}],
            [{"text": "🔙 Avto javoblar ro‘yxati", "callback_data": "menu:auto_replies"}],
        ]}

    def _list_auto_replies(self, user_id: int | None) -> list[dict[str, object]]:
        if not isinstance(user_id, int):
            return []
        getter = getattr(self.store, "list_auto_replies", None)
        try:
            rows = getter(user_id) if callable(getter) else []
            return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception as exc:
            LOGGER.warning("Auto-reply list o‘qilmadi user=%s: %s", user_id, exc)
            return []

    def _get_auto_reply(self, user_id: int | None, record_id: int | None) -> dict[str, object] | None:
        if not isinstance(user_id, int) or record_id is None:
            return None
        getter = getattr(self.store, "get_auto_reply", None)
        try:
            row = getter(user_id, record_id) if callable(getter) else None
            return dict(row) if isinstance(row, dict) else None
        except Exception as exc:
            LOGGER.warning("Auto-reply o‘qilmadi user=%s: %s", user_id, exc)
            return None

    def _delete_auto_reply(self, user_id: int, record_id: int) -> bool:
        deleter = getattr(self.store, "delete_auto_reply", None)
        try:
            return bool(callable(deleter) and deleter(user_id, record_id))
        except Exception as exc:
            LOGGER.warning("Auto-reply o‘chirilmadi user=%s: %s", user_id, exc)
            return False

    @staticmethod
    def _parse_record_id(data: str) -> int | None:
        try:
            return int(data.rsplit(":", 1)[-1])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _about_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "🔙 Asosiy menyu", "callback_data": "menu:home"}]]}

    @staticmethod
    def _premium_role_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "🔙 VIP", "callback_data": "premium:status"}],
            [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}],
        ]}

    def _premium_keyboard(self, active: bool) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        if active:
            rows.append([{ "text": "🧠 Shaxsiy rol", "callback_data": "premium:role"}])
        else:
            rows.append([{"text": "⭐ 100 Stars bilan obuna", "callback_data": "premium:buy"}])
        rows.append([{"text": "🔄 Statusni yangilash", "callback_data": "premium:status"}])
        rows.append([{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}])
        return {"inline_keyboard": rows}

    async def _handle_mangekyo_promo(self, message: dict[str, Any], chat_id: int) -> None:
        user_id = self._user_id(message)
        if user_id is None:
            return
        started_method = getattr(self.store, "has_started", None)
        if callable(started_method) and not started_method(user_id):
            await self._send_chunks(chat_id, PROMO_SILENT_REPLY, None, message.get("message_id"))
            return
        redeem = getattr(self.store, "redeem_promo", None)
        if not callable(redeem) or not redeem(user_id, MANGEKYO_PROMO_CODE, time.time() + STAR_SUBSCRIPTION_PERIOD_SECONDS):
            await self._send_chunks(chat_id, PROMO_SILENT_REPLY, None, message.get("message_id"))
            return
        grant = getattr(self.store, "grant_premium", None)
        if callable(grant):
            grant(user_id, time.time() + STAR_SUBSCRIPTION_PERIOD_SECONDS, "mangekyo_promo")
        await self._send_chunks(chat_id, MANGEKYO_PROMO_REPLY, None, message.get("message_id"))

    async def _handle_premium_role(self, message: dict[str, Any], argument: str, chat_id: int, reply_to: int | None) -> None:
        user_id = self._user_id(message)
        if user_id is None or not self._has_premium(user_id):
            await self._send_chunks(chat_id, "Bu funksiya faqat premium userlar uchun. /premium buyrug‘i orqali obuna bo‘ling.", None, reply_to)
            return
        getter = getattr(self.store, "get_user_role", None)
        setter = getattr(self.store, "set_user_role", None)
        clearer = getattr(self.store, "clear_user_role", None)
        if not callable(getter) or not callable(setter) or not callable(clearer):
            await self._send_chunks(chat_id, "Shaxsiy rol storage’i hozircha mavjud emas.", None, reply_to)
            return
        if not argument:
            current = getter(user_id, "")
            status = current or "Qo‘shimcha shaxsiy rol berilmagan; AI oddiy javob rejimida ishlaydi."
            await self._send_chunks(chat_id, f"Shaxsiy rol:\n{status}", None, reply_to)
            return
        if argument.casefold() in {"reset", "default", "tozalash"}:
            clearer(user_id)
            await self._send_chunks(chat_id, "Shaxsiy qo‘shimcha rol olib tashlandi; AI oddiy javob rejimida ishlaydi.", None, reply_to)
            return
        if len(argument) > 2000:
            await self._send_chunks(chat_id, "Shaxsiy rol 2000 belgidan oshmasligi kerak.", None, reply_to)
            return
        setter(user_id, argument)
        await self._send_chunks(chat_id, "Shaxsiy premium rolingiz saqlandi.", None, reply_to)

    async def handle_pre_checkout_query(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        payload = str(query.get("invoice_payload") or "")
        if not isinstance(query_id, str):
            return
        if payload != STAR_SUBSCRIPTION_PAYLOAD:
            await self.telegram.answer_pre_checkout_query(query_id, False, "Invoice ma’lumotlari yaroqsiz.")
            return
        await self.telegram.answer_pre_checkout_query(query_id, True)

    async def handle_successful_payment(self, message: dict[str, Any]) -> None:
        payment = message.get("successful_payment") or {}
        user_id = self._user_id(message)
        chat_id = self._chat_id(message)
        if user_id is None or chat_id is None:
            return
        amount = int(payment.get("total_amount") or 0)
        if payment.get("currency") != "XTR" or payment.get("invoice_payload") != STAR_SUBSCRIPTION_PAYLOAD or amount != STAR_SUBSCRIPTION_AMOUNT:
            LOGGER.warning("Noma’lum yoki noto‘g‘ri summadagi successful_payment qabul qilindi")
            return
        charge_id = str(payment.get("telegram_payment_charge_id") or "")
        if not charge_id:
            return
        expiration = float(payment.get("subscription_expiration_date") or (time.time() + STAR_SUBSCRIPTION_PERIOD_SECONDS))
        recorder = getattr(self.store, "record_star_payment", None)
        inserted = recorder(
            charge_id=charge_id,
            user_id=user_id,
            amount=amount,
            currency=str(payment.get("currency")),
            invoice_payload=str(payment.get("invoice_payload")),
            subscription_expiration_date=expiration,
            is_recurring=bool(payment.get("is_recurring")),
            is_first_recurring=bool(payment.get("is_first_recurring")),
        ) if callable(recorder) else True
        if inserted:
            grant = getattr(self.store, "grant_premium", None)
            if callable(grant):
                grant(user_id, expiration, "stars_subscription")
        await self._send_chunks(chat_id, "✅ To‘lov tasdiqlandi. Premium funksiyalar 30 kunga faollashdi. /premium orqali statusni ko‘ring.", None, message.get("message_id"))

    async def handle_subscription_update(self, update: dict[str, Any]) -> None:
        user = update.get("user") or {}
        user_id = user.get("id")
        state = str(update.get("state") or "")
        setter = getattr(self.store, "set_subscription_state", None)
        if isinstance(user_id, int) and callable(setter) and state:
            setter(user_id, state)

    def _admin_panel_text(self) -> str:
        return "👮 Admin panel\n\nKerakli bo‘limni tanlang:"

    def _admin_panel_keyboard(self, include_statistics: bool = True, include_main_menu: bool = False, user_id: int | None = None, include_owner_tools: bool = False) -> dict[str, Any]:
        pause_label = "⏱ Pause: YOQILGAN" if self._manual_pause_enabled(user_id) else "⏱ Pause: O‘CHIRILGAN"
        rows: list[list[dict[str, str]]] = []
        if include_statistics:
            rows.append([{"text": "📊 Statistika", "callback_data": "admin:stats"}])
        rows.extend([
            [{"text": "🧠 AI roli", "callback_data": "admin:role"}],
            [{"text": pause_label, "callback_data": "admin:pause"}],
        ])
        if include_owner_tools:
            rows.extend([
                [{"text": "💎 VIP boshqaruvi", "callback_data": "owner:vip"}],
                [{"text": "📢 Kanal boshqaruvi", "callback_data": "owner:channels"}],
                [{"text": "✉️ Xabar yuborish", "callback_data": "owner:broadcast"}],
                [{"text": "🖼 Menyu media sozlamalari", "callback_data": "owner:media"}],
            ])
        if include_main_menu:
            rows.append([{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}])
        return {"inline_keyboard": rows}

    def _admin_pause_keyboard(self, enabled: bool) -> dict[str, Any]:
        toggle_label = "⏸ O‘chirish" if enabled else "▶️ Yoqish"
        return {"inline_keyboard": [
            [{"text": toggle_label, "callback_data": "admin:pause:toggle"}],
            [{"text": "🔙 Admin panel", "callback_data": "admin:home"}],
            [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}],
        ]}

    def _manual_pause_enabled(self, user_id: int | None = None) -> bool:
        if isinstance(user_id, int) and user_id != OWNER_ADMIN_ID:
            user_method = getattr(self.store, "user_manual_pause_enabled", None)
            if callable(user_method):
                try:
                    return bool(user_method(user_id, True))
                except Exception:
                    LOGGER.exception("User pause holatini o‘qib bo‘lmadi")
                    return True
        method = getattr(self.store, "manual_pause_enabled", None)
        if not callable(method):
            return True
        try:
            return bool(method(True))
        except Exception:
            LOGGER.exception("Manual pause holatini o‘qib bo‘lmadi")
            return True

    def _set_manual_pause_enabled(self, enabled: bool, user_id: int | None = None) -> None:
        if isinstance(user_id, int) and user_id != OWNER_ADMIN_ID:
            user_method = getattr(self.store, "set_user_manual_pause_enabled", None)
            if callable(user_method):
                try:
                    user_method(user_id, bool(enabled))
                    return
                except Exception:
                    LOGGER.exception("User pause holatini saqlab bo‘lmadi")
                    return
        method = getattr(self.store, "set_manual_pause_enabled", None)
        if not callable(method):
            return
        try:
            method(bool(enabled))
        except Exception:
            LOGGER.exception("Manual pause holatini saqlab bo‘lmadi")

    def _admin_back_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "🔙 Admin panel", "callback_data": "admin:home"}],
            [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}],
        ]}

    def _admin_stats_text(self) -> str:
        if hasattr(self.store, "conversation_count"):
            chats = int(self.store.conversation_count())
        else:
            chats = len(getattr(self.store, "data", {}))
        if hasattr(self.store, "pause_count"):
            pauses = int(self.store.pause_count())
        else:
            pauses = len(getattr(self.store, "owner_activity", {}))
        provider = getattr(self.settings, "ai_provider", "unknown")
        pause_state = "yoqilgan" if self._manual_pause_enabled() else "o‘chirilgan"
        premium_method = getattr(self.store, "premium_count", None)
        premium = int(premium_method()) if callable(premium_method) else 0
        return f"📊 Statistika\n\n💬 Xotiradagi chatlar: {chats}\n⭐ Faol premium userlar: {premium}\n⏱ Pause yozuvlari: {pauses}\n⏱ Manual pause: {pause_state}\n🤖 AI provider: {provider}"

    @staticmethod
    def _is_apk_message(message: dict[str, Any]) -> bool:
        document = message.get("document") or {}
        if not isinstance(document, dict):
            return False
        filename = str(document.get("file_name") or "").lower()
        mime_type = str(document.get("mime_type") or "").lower()
        return filename.endswith(".apk") or mime_type == "application/vnd.android.package-archive"

    async def _delete_business_apk(self, message: dict[str, Any], business_connection_id: str) -> None:
        message_id = message.get("message_id")
        chat_id = self._chat_id(message)
        if not isinstance(message_id, int) or not isinstance(chat_id, int):
            return
        try:
            await self.telegram.delete_business_messages(business_connection_id, [message_id])
            LOGGER.info("APK Business xabari ikki tomon uchun o‘chirildi: chat_id=%s message_id=%s", chat_id, message_id)
        except TelegramApiError as exc:
            LOGGER.error("APK xabarini o‘chirib bo‘lmadi: %s", exc)

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
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)] or ["…"]
        for index, chunk in enumerate(chunks):
            await self.telegram.send_message(
                chat_id=chat_id,
                text=chunk,
                business_connection_id=business_connection_id,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
                reply_markup=reply_markup if index == 0 else None,
            )


    def _user_session(self, user_id: int | None) -> dict[str, Any] | None:
        if not isinstance(user_id, int):
            return None
        getter = getattr(self.store, "get_admin_session", None)
        session = getter(user_id) if callable(getter) else None
        return session if isinstance(session, dict) else None

    def _set_user_session(self, user_id: int | None, state: str, data: dict[str, Any] | None = None) -> None:
        if not isinstance(user_id, int):
            return
        setter = getattr(self.store, "set_admin_session", None)
        if callable(setter):
            setter(user_id, state, data or {})

    def _clear_user_session(self, user_id: int | None) -> None:
        if not isinstance(user_id, int):
            return
        clearer = getattr(self.store, "clear_admin_session", None)
        if callable(clearer):
            clearer(user_id)

    def _owner_session(self, user_id: int | None) -> dict[str, Any] | None:
        if user_id != OWNER_ADMIN_ID:
            return None
        getter = getattr(self.store, "get_admin_session", None)
        session = getter(user_id) if callable(getter) else None
        return session if isinstance(session, dict) else None

    def _set_owner_session(self, user_id: int | None, state: str, data: dict[str, Any] | None = None) -> None:
        if user_id != OWNER_ADMIN_ID:
            return
        setter = getattr(self.store, "set_admin_session", None)
        if callable(setter):
            setter(user_id, state, data or {})

    def _clear_owner_session(self, user_id: int | None) -> None:
        if user_id != OWNER_ADMIN_ID:
            return
        clearer = getattr(self.store, "clear_admin_session", None)
        if callable(clearer):
            clearer(user_id)

    async def _handle_auto_reply_session(self, message: dict[str, Any], text: str, chat_id: int, session: dict[str, Any] | None) -> bool:
        user_id = self._user_id(message)
        if not isinstance(user_id, int) or not session:
            return False
        state = str(session.get("state") or "")
        data = session.get("data") if isinstance(session.get("data"), dict) else {}
        if text.casefold() in {"/cancel", "bekor"}:
            self._clear_user_session(user_id)
            await self._send_chunks(chat_id, "✅ Amal bekor qilindi.", None, message.get("message_id"), self._auto_reply_back_keyboard())
            return True
        if state == "auto_add_trigger":
            if not text.strip() or len(text.strip()) > 200:
                await self._send_chunks(chat_id, "Trigger 1–200 belgi bo‘lishi kerak.", None, message.get("message_id"))
                return True
            self._set_user_session(user_id, "auto_add_response", {"trigger": text.strip()})
            await self._send_chunks(chat_id, "✅ Trigger saqlandi. Endi unga yuboriladigan javob matnini yuboring.", None, message.get("message_id"))
            return True
        if state == "auto_add_response":
            trigger = str(data.get("trigger") or "").strip()
            if not trigger or not text.strip():
                await self._send_chunks(chat_id, "Trigger va javob bo‘sh bo‘lmasligi kerak.", None, message.get("message_id"))
                return True
            writer = getattr(self.store, "upsert_auto_reply", None)
            record_id = writer(user_id, trigger, text.strip()) if callable(writer) else 0
            self._clear_user_session(user_id)
            await self._send_chunks(chat_id, f"✅ Avto javob saqlandi (ID: {record_id}).", None, message.get("message_id"), self._auto_reply_back_keyboard())
            return True
        if state == "auto_edit":
            record_id = int(data.get("record_id", 0) or 0)
            if "|" not in text:
                await self._send_chunks(chat_id, "Format: trigger | javob", None, message.get("message_id"))
                return True
            trigger, response = (part.strip() for part in text.split("|", 1))
            writer = getattr(self.store, "upsert_auto_reply", None)
            record_id = writer(user_id, trigger, response, record_id) if callable(writer) else 0
            self._clear_user_session(user_id)
            await self._send_chunks(chat_id, f"✅ Avto javob yangilandi (ID: {record_id}).", None, message.get("message_id"), self._auto_reply_back_keyboard())
            return True
        return False

    async def _handle_builtin_auto_command(self, message: dict[str, Any], text: str, chat_id: int, is_business: bool, business_owner_id: int | None) -> bool:
        if not text.startswith("."):
            return False
        parts = text[1:].split(maxsplit=1)
        command = (parts[0].split("@", 1)[0].casefold() if parts else "")
        argument = parts[1].strip() if len(parts) == 2 else ""
        user_id = self._user_id(message)
        owner_id = business_owner_id if is_business else user_id
        if command not in AUTO_REPLY_COMMANDS or not isinstance(owner_id, int):
            return False
        connection_id = message.get("business_connection_id") if is_business else None
        if self._user_setting(owner_id, f"auto_reply_{command}_permission", "all") != "all":
            await self._send_chunks(chat_id, f".{command} buyrug‘i hozir o‘chirilgan.", connection_id, message.get("message_id"))
            return True
        reply_to = message.get("message_id")
        if command == "help":
            await self._send_chunks(chat_id, COMMANDS_PAGE_1_TEXT, connection_id, reply_to)
        elif command == "ping":
            await self._send_chunks(chat_id, "🏓 Pong!", connection_id, reply_to)
        elif command == "settings":
            await self._send_chunks(chat_id, self._settings_text(owner_id), connection_id, reply_to, self._settings_keyboard(owner_id))
        elif command == "list":
            rows = self._list_auto_replies(owner_id)
            body = "💬 Avto javoblar ro‘yxati\n\n" + ("\n".join(f"{row.get('id')}. {row.get('trigger')} → {row.get('response')}" for row in rows) if rows else "Hozircha ro‘yxat bo‘sh.")
            await self._send_chunks(chat_id, body, connection_id, reply_to)
        elif command in {"add", "edit", "delete"}:
            if is_business:
                await self._send_chunks(chat_id, "Bu boshqaruv buyrug‘i faqat akkaunt egasi chatida ishlaydi.", connection_id, reply_to)
            elif command == "add":
                if "|" in argument:
                    trigger, response = (part.strip() for part in argument.split("|", 1))
                    writer = getattr(self.store, "upsert_auto_reply", None)
                    record_id = writer(owner_id, trigger, response) if callable(writer) and trigger and response else 0
                    await self._send_chunks(chat_id, f"✅ Avto javob saqlandi (ID: {record_id})." if record_id else "Format: .add trigger | javob", None, reply_to)
                else:
                    self._set_user_session(owner_id, "auto_add_trigger")
                    await self._send_chunks(chat_id, "➕ Trigger so‘z yoki iborani yuboring. Bekor qilish: /cancel", None, reply_to, self._auto_reply_back_keyboard())
            elif command == "edit":
                edit_parts = argument.split(maxsplit=1)
                try:
                    record_id = int(edit_parts[0])
                except (IndexError, ValueError):
                    record_id = 0
                if record_id and len(edit_parts) == 2 and "|" in edit_parts[1]:
                    trigger, response = (part.strip() for part in edit_parts[1].split("|", 1))
                    writer = getattr(self.store, "upsert_auto_reply", None)
                    updated_id = writer(owner_id, trigger, response, record_id) if callable(writer) and trigger and response else 0
                    await self._send_chunks(chat_id, f"✅ Avto javob yangilandi (ID: {updated_id})." if updated_id else "Avto javob topilmadi.", None, reply_to)
                else:
                    await self._send_chunks(chat_id, "✏️ Format: .edit ID trigger | javob", None, reply_to)
            else:
                try:
                    record_id = int(argument)
                except ValueError:
                    record_id = 0
                deleted = self._delete_auto_reply(owner_id, record_id) if record_id else False
                await self._send_chunks(chat_id, "✅ Avto javob o‘chirildi." if deleted else "Avto javob topilmadi.", None, reply_to)
        elif command == "info":
            sender = message.get("from") or {}
            who = self._display_name(sender)
            target = f"Business owner ID: {business_owner_id}" if is_business else f"Telegram ID: {user_id}"
            await self._send_chunks(chat_id, f"👥 Suhbatdosh: {who}\n{target}", connection_id, reply_to)
        elif command == "type":
            await self._send_chunks(chat_id, argument or "📝 Matn kiriting.", connection_id, reply_to)
        elif command == "down":
            await self._send_chunks(chat_id, "📥 .down uchun yuklanadigan havolani yuboring." if not argument else "📥 Havola qabul qilindi. Bu deploymentda avtomatik yuklash xizmati ulanmagan.", connection_id, reply_to)
        elif command == "music":
            await self._send_chunks(chat_id, "🎵 .music uchun musiqa nomi yoki havola yuboring. Bu deploymentda audio qidiruv xizmati ulanmagan.", connection_id, reply_to)
        elif command == "checklist":
            items = [item.strip() for item in argument.split(",") if item.strip()] if argument else []
            await self._send_chunks(chat_id, "☑️ Checklist\n\n" + ("\n".join(f"☐ {item}" for item in items) if items else "☐ Bandlarni vergul bilan yuboring."), connection_id, reply_to)
        elif command == "ai":
            if not argument:
                await self._send_chunks(chat_id, "🤖 .ai dan keyin savol yozing.", connection_id, reply_to)
            else:
                try:
                    answer, _provider = await self.ai.answer([{"role": "user", "content": argument}])
                    await self._send_chunks(chat_id, answer, connection_id, reply_to)
                except ProviderError:
                    await self._send_chunks(chat_id, "AI javobini tayyorlab bo‘lmadi.", connection_id, reply_to)
        elif command == "emoji":
            await self._send_chunks(chat_id, f"🌟 {argument}" if argument else "🌟 Matn kiriting.", connection_id, reply_to)
        elif command == "dice":
            await self._send_chunks(chat_id, "🎲", connection_id, reply_to)
        return True

    async def _handle_owner_session(self, message: dict[str, Any], text: str, chat_id: int) -> bool:
        user_id = self._user_id(message)
        session = self._owner_session(user_id)
        if not session:
            return False
        state = str(session.get("state") or "")
        data = session.get("data") if isinstance(session.get("data"), dict) else {}
        reply_to = message.get("message_id")
        if text.casefold() in {"bekor", "/cancel"}:
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, "✅ Amal bekor qilindi.", None, reply_to, self._admin_panel_keyboard(include_statistics=user_id == OWNER_ADMIN_ID, include_main_menu=True, user_id=user_id, include_owner_tools=user_id == OWNER_ADMIN_ID))
            return True
        if state == "media_upload":
            slot = str(data.get("slot") or "")
            config = MEDIA_SLOTS.get(slot)
            expected_type = config[3] if config else ""
            file_id = ""
            media_type = ""
            if isinstance(message.get("photo"), list) and message.get("photo"):
                photos = [item for item in message["photo"] if isinstance(item, dict) and item.get("file_id")]
                if photos:
                    file_id = str(photos[-1]["file_id"])
                    media_type = "photo"
            elif isinstance(message.get("video"), dict) and message["video"].get("file_id"):
                file_id = str(message["video"]["file_id"])
                media_type = "video"
            if not config or not file_id:
                await self._send_chunks(chat_id, "❌ Rasm yoki video yuboring. Amalni bekor qilish: /cancel", None, reply_to, self._owner_media_keyboard())
                return True
            if media_type != expected_type:
                expected_label = "rasm" if expected_type == "photo" else "video"
                await self._send_chunks(chat_id, f"❌ Bu bo‘lim uchun {expected_label} yuborish kerak. Amalni bekor qilish: /cancel", None, reply_to, self._owner_media_keyboard())
                return True
            file_key, type_key, label, _ = config
            self._set_setting(file_key, file_id)
            self._set_setting(type_key, media_type)
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, f"✅ {label} saqlandi.", None, reply_to, self._owner_media_keyboard())
            return True
        if state == "vip_grant_id":
            try:
                target_id = int(text)
            except ValueError:
                await self._send_chunks(chat_id, "❌ Telegram user ID faqat raqam bo‘lishi kerak.", None, reply_to)
                return True
            self._set_owner_session(user_id, "vip_grant_days", {"user_id": target_id})
            await self._send_chunks(chat_id, "VIP necha kunga berilsin? Masalan: 30", None, reply_to)
            return True
        if state == "vip_grant_days":
            try:
                days = int(text)
                if days < 1 or days > 3650:
                    raise ValueError
            except ValueError:
                await self._send_chunks(chat_id, "❌ Muddat 1–3650 kun oralig‘ida raqam bo‘lishi kerak.", None, reply_to)
                return True
            target_id = int(data.get("user_id", 0))
            grant = getattr(self.store, "grant_vip_days", None)
            if callable(grant):
                grant(target_id, days)
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, f"✅ {target_id} userga {days} kunlik VIP berildi.", None, reply_to, self._owner_vip_keyboard())
            return True
        if state == "vip_revoke_id":
            try:
                target_id = int(text)
            except ValueError:
                await self._send_chunks(chat_id, "❌ Telegram user ID faqat raqam bo‘lishi kerak.", None, reply_to)
                return True
            revoke = getattr(self.store, "revoke_vip", None)
            if callable(revoke):
                revoke(target_id)
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, f"✅ {target_id} userning VIP accessi olib tashlandi.", None, reply_to, self._owner_vip_keyboard())
            return True
        if state in {"channel_add_public", "channel_add_main", "channel_add_required"}:
            try:
                channel_type = {"channel_add_public": "public", "channel_add_main": "main", "channel_add_required": "required"}[state]
                chat = await self.telegram.get_chat(text)
                channel_id = str(chat.get("id"))
                if channel_id == "None":
                    raise TelegramApiError("getChat", "chat ID topilmadi")
                saver = getattr(self.store, "upsert_channel", None)
                if callable(saver):
                    saver(channel_id, str(chat.get("title") or chat.get("first_name") or ""), str(chat.get("username") or ""), channel_type, channel_type == "required", channel_type == "main")
                self._clear_owner_session(user_id)
                await self._send_chunks(chat_id, "✅ Kanal saqlandi. Bot kanalga xabar yuborishi uchun kanalda admin huquqi bo‘lishi kerak.", None, reply_to, self._owner_channels_keyboard())
            except (TelegramApiError, ValueError) as exc:
                await self._send_chunks(chat_id, f"❌ Kanal topilmadi yoki saqlanmadi: {exc}", None, reply_to, self._owner_channels_keyboard())
            return True
        if state == "channel_add_url":
            value = text.strip()
            if not (value.startswith("http://") or value.startswith("https://")):
                await self._send_chunks(chat_id, "❌ URL http:// yoki https:// bilan boshlanishi kerak.", None, reply_to, self._owner_channels_keyboard())
                return True
            channel_id = "url:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
            saver = getattr(self.store, "upsert_channel", None)
            if callable(saver):
                saver(channel_id, value, "", "url", True, False, "", value)
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, "✅ Oddiy URL saqlandi.", None, reply_to, self._owner_channels_keyboard())
            return True
        if state == "channel_add_private_forward":
            origin = message.get("forward_origin") or {}
            forwarded_chat = message.get("forward_from_chat") or (origin.get("chat") if isinstance(origin, dict) else {}) or {}
            if not isinstance(forwarded_chat, dict) or not forwarded_chat.get("id"):
                await self._send_chunks(chat_id, "❌ Kanal yoki guruhdan forward qilingan xabar yuboring.", None, reply_to, self._owner_channels_keyboard())
                return True
            self._set_owner_session(user_id, "channel_add_private_link", {
                "chat_id": str(forwarded_chat.get("id")),
                "title": str(forwarded_chat.get("title") or ""),
                "username": str(forwarded_chat.get("username") or ""),
            })
            await self._send_chunks(chat_id, "🔐 Endi shu private kanalning invite linkini yuboring:\nhttps://t.me/+... yoki https://t.me/joinchat/...", None, reply_to, self._owner_channels_keyboard())
            return True
        if state == "channel_add_private_link":
            value = text.strip()
            if not (value.startswith("https://t.me/+") or value.startswith("https://t.me/joinchat/")):
                await self._send_chunks(chat_id, "❌ Private kanal invite linki noto‘g‘ri.", None, reply_to, self._owner_channels_keyboard())
                return True
            saver = getattr(self.store, "upsert_channel", None)
            if callable(saver):
                saver(str(data.get("chat_id")), str(data.get("title") or ""), str(data.get("username") or ""), "private", True, False, value, "")
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, "✅ Private kanal saqlandi.", None, reply_to, self._owner_channels_keyboard())
            return True
        if state == "channel_delete":
            deleter = getattr(self.store, "delete_channel", None)
            if callable(deleter):
                deleter(text)
            self._clear_owner_session(user_id)
            await self._send_chunks(chat_id, "✅ Kanal ro‘yxatdan o‘chirildi.", None, reply_to, self._owner_channels_keyboard())
            return True
        if state == "broadcast_one_id":
            try:
                target_id = int(text)
            except ValueError:
                await self._send_chunks(chat_id, "❌ Telegram user ID faqat raqam bo‘lishi kerak.", None, reply_to)
                return True
            self._set_owner_session(user_id, "broadcast_type", {"target": "one", "user_id": target_id})
            await self._send_chunks(chat_id, "📨 Yuborish usulini tanlang.", None, reply_to, self._owner_broadcast_type_keyboard("one"))
            return True
        if state == "broadcast_text":
            target = str(data.get("target") or "all")
            recipients_override = data.get("chat_ids") if target == "channels" else ([int(data.get("user_id"))] if target == "one" and data.get("user_id") else None)
            self._clear_owner_session(user_id)
            sent, failed, total = await self._run_owner_broadcast(target, text, recipients_override)
            await self._send_chunks(chat_id, f"📤 Xabar yuborish tugadi.\n\nJami: {total}\n✅ Yuborildi: {sent}\n❌ Xato: {failed}", None, reply_to, self._owner_broadcast_keyboard())
            return True
        if state == "broadcast_forward":
            target = str(data.get("target") or "all")
            recipients_override = data.get("chat_ids") if target == "channels" else ([int(data.get("user_id"))] if target == "one" and data.get("user_id") else None)
            source_message_id = message.get("message_id")
            self._clear_owner_session(user_id)
            sent, failed, total = await self._run_owner_forward(target, chat_id, source_message_id, recipients_override)
            await self._send_chunks(chat_id, f"📤 Forward yuborish tugadi.\n\nJami: {total}\n✅ Yuborildi: {sent}\n❌ Xato: {failed}", None, reply_to, self._owner_broadcast_keyboard())
            return True
        self._clear_owner_session(user_id)
        return False

    async def _run_owner_broadcast(self, target: str, text: str, recipients_override: list[object] | None = None) -> tuple[int, int, int]:
        if recipients_override is not None:
            recipients = [str(item) if target == "channels" else int(item) for item in recipients_override]
        elif target == "channels":
            getter = getattr(self.store, "list_channels", None)
            recipients = [str(row.get("chat_id")) for row in (getter() if callable(getter) else []) if row.get("chat_id")]
        else:
            getter = getattr(self.store, "broadcast_user_ids", None)
            recipients = [int(user_id) for user_id in (getter(target) if callable(getter) else [])]
        sent = 0
        failed = 0
        for recipient in recipients[:5000]:
            try:
                await self.telegram.send_message(chat_id=recipient, text=text)
                sent += 1
            except TelegramApiError as exc:
                failed += 1
                LOGGER.warning("Owner broadcast yuborilmadi recipient=%s: %s", recipient, exc)
        return sent, failed, len(recipients)

    async def _run_owner_forward(self, target: str, source_chat_id: int, source_message_id: int | None, recipients_override: list[object] | None = None) -> tuple[int, int, int]:
        if not isinstance(source_message_id, int):
            return 0, 1, 1
        if recipients_override is not None:
            recipients = [str(item) if target == "channels" else int(item) for item in recipients_override]
        elif target == "channels":
            getter = getattr(self.store, "list_channels", None)
            recipients = [str(row.get("chat_id")) for row in (getter() if callable(getter) else []) if row.get("chat_id")]
        else:
            getter = getattr(self.store, "broadcast_user_ids", None)
            recipients = [int(user_id) for user_id in (getter(target) if callable(getter) else [])]
        sent = 0
        failed = 0
        for recipient in recipients[:5000]:
            try:
                await self.telegram.forward_message(recipient, source_chat_id, source_message_id)
                sent += 1
            except TelegramApiError as exc:
                failed += 1
                LOGGER.warning("Owner forward yuborilmadi recipient=%s: %s", recipient, exc)
        return sent, failed, len(recipients)

    def _owner_vip_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "➕ VIP berish", "callback_data": "vip:grant"}, {"text": "❌ VIP olish", "callback_data": "vip:revoke"}],
            [{"text": "📋 VIP userlar", "callback_data": "vip:list"}],
            [{"text": "🔙 Admin panel", "callback_data": "admin:home"}],
        ]}

    def _owner_channel_type_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "📢 Ommaviy kanal", "callback_data": "channel:type:public"}],
            [{"text": "⭐ Asosiy kanal", "callback_data": "channel:type:main"}],
            [{"text": "🔐 Majburiy obuna kanali", "callback_data": "channel:type:required"}],
            [{"text": "🔒 Private/so‘rovli kanal", "callback_data": "channel:type:private"}],
            [{"text": "🌐 Oddiy URL", "callback_data": "channel:type:url"}],
            [{"text": "🔙 Kanal boshqaruvi", "callback_data": "owner:channels"}],
        ]}

    def _owner_channels_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "➕ Kanal qo‘shish", "callback_data": "channel:add"}],
            [{"text": "📋 Kanallar ro‘yxati", "callback_data": "channel:list"}],
            [{"text": "🗑 Kanalni o‘chirish", "callback_data": "channel:delete"}],
            [{"text": "🔙 Admin panel", "callback_data": "admin:home"}],
        ]}

    def _owner_broadcast_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "👤 Bitta userga", "callback_data": "broadcast:one"}],
            [{"text": "👥 Barcha userlarga", "callback_data": "broadcast:all"}],
            [{"text": "💎 VIP userlarga", "callback_data": "broadcast:vip"}],
            [{"text": "⭐ Oddiy userlarga", "callback_data": "broadcast:normal"}],
            [{"text": "📢 Tanlangan kanallarga", "callback_data": "broadcast:channels"}],
            [{"text": "🔙 Admin panel", "callback_data": "admin:home"}],
        ]}

    def _owner_broadcast_type_keyboard(self, target: str, chat_ids: list[str] | None = None) -> dict[str, Any]:
        encoded_target = target
        if target == "channels":
            encoded_target = "channels|" + ",".join(chat_ids or [])
        return {"inline_keyboard": [
            [{"text": "✍️ Matn yuborish", "callback_data": f"broadcast:type:text:{encoded_target}"}],
            [{"text": "↗️ Forward xabar yuborish", "callback_data": f"broadcast:type:forward:{encoded_target}"}],
            [{"text": "🔙 Xabar yuborish", "callback_data": "owner:broadcast"}],
        ]}

    def _owner_broadcast_channel_select_keyboard(self, selected: list[str]) -> dict[str, Any]:
        getter = getattr(self.store, "list_channels", None)
        channels = getter() if callable(getter) else []
        rows: list[list[dict[str, str]]] = []
        for row in channels[:50]:
            channel_id = str(row.get("chat_id") or "")
            if not channel_id or str(row.get("channel_type") or "") == "url":
                continue
            mark = "✅" if channel_id in selected else "☑️"
            label = row.get("username") or row.get("title") or channel_id
            rows.append([{"text": f"{mark} {label}", "callback_data": f"broadcast:toggle:{channel_id}"}])
        rows.append([{ "text": "🚀 Tanlanganlarga yuborish", "callback_data": "broadcast:send_selected"}])
        rows.append([{ "text": "🔙 Xabar yuborish", "callback_data": "owner:broadcast"}])
        return {"inline_keyboard": rows}

    def _owner_media_text(self) -> str:
        lines = ["🖼 Menyu media sozlamalari\n", "Start va Buyruqlar uchun rasm, Qo‘llanma bo‘limlari uchun video yuboring."]
        for slot, (_file_key, _type_key, label, _expected) in MEDIA_SLOTS.items():
            state = "✅ sozlangan" if self._media_config(slot) else "— sozlanmagan"
            lines.append(f"\n{label}: {state}")
        return "".join(lines)

    def _owner_media_keyboard(self) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        for slot, (_file_key, _type_key, label, _expected) in MEDIA_SLOTS.items():
            rows.append([{"text": f"📤 {label}", "callback_data": f"owner:media:set:{slot}"}])
            if self._media_config(slot):
                rows.append([{"text": f"🗑 {label}ni o‘chirish", "callback_data": f"owner:media:remove:{slot}"}])
        rows.append([{"text": "🔙 Admin panel", "callback_data": "admin:home"}])
        return {"inline_keyboard": rows}

    async def _edit_owner_screen(self, chat_id: int, message_id: int, text: str, markup: dict[str, Any]) -> None:
        try:
            await self.telegram.edit_message_text(chat_id, message_id, text, markup)
        except TelegramApiError:
            await self._send_chunks(chat_id, text, None, None, markup)

    def _owner_vip_text(self) -> str:
        getter = getattr(self.store, "list_vip_users", None)
        users = getter() if callable(getter) else []
        if not users:
            return "💎 VIP boshqaruvi\n\nHozircha faol VIP userlar yo‘q."
        lines = ["💎 VIP boshqaruvi\n"]
        for row in users[:100]:
            until = time.strftime("%Y-%m-%d", time.localtime(float(row.get("premium_until", 0))))
            lines.append(f"• {row.get('user_id')} — {until}")
        return "\n".join(lines)

    def _owner_channels_text(self) -> str:
        getter = getattr(self.store, "list_channels", None)
        channels = getter() if callable(getter) else []
        if not channels:
            return "📢 Kanal boshqaruvi\n\nSaqlangan kanallar yo‘q."
        lines = ["📢 Kanal boshqaruvi\n"]
        type_labels = {"public": "Ommaviy", "main": "Asosiy", "required": "Majburiy", "private": "Private", "url": "URL"}
        for row in channels[:100]:
            label = row.get("username") or row.get("title") or row.get("chat_id")
            kind = type_labels.get(str(row.get("channel_type") or "public"), str(row.get("channel_type") or "public"))
            extra = f" — {row.get('invite_link')}" if row.get("invite_link") else ""
            lines.append(f"• [{kind}] {label} — {row.get('chat_id')}{extra}")
        return "\n".join(lines)

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
