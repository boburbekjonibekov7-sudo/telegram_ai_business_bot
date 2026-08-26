from __future__ import annotations

import asyncio
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
START_MENU_TEXT = "Salom! Telegram Business chatlaringizga avtomatik javob beruvchi AI CHAT BOT man✨\n\nBotdan to‘liq foydalanish uchun PREMIUM 💎 oling"
BOT_ABOUT_TEXT = "Bot haqida 🤖\n\n• Telegram Business va Chat Automation chatlariga AI javob beradi.\n• Business chatda yuborilgan APK fayllarni avtomatik o‘chirishni qo‘llab-quvvatlaydi.\n\nPremium 💎 imkoniyatlari:\n• Oyiga 100 Telegram Stars evaziga 30 kunlik access.\n• Shaxsiy AI chat va shaxsiy rol sozlamalari.\n• Kengaytirilgan admin panel va pause boshqaruvi.\n• To‘lovdan keyin premium funksiyalar avtomatik ochiladi."



class BusinessAiBot:
    def __init__(self, settings: Settings, store: Any | None = None):
        self.settings = settings
        self.telegram = TelegramBotApi(settings.bot_token)
        self.ai = AIService(settings)
        self.store = store or PostgresStore.from_env(settings.max_history_messages) or JsonStore(settings.data_dir, settings.max_history_messages)
        self.pause_store = None
        self.connections: dict[str, dict[str, Any]] = {}
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
            # Editing an incoming message should not create a second AI answer.
            return
        if "deleted_business_messages" in update:
            return
        if "message" in update:
            message = update["message"]
            if message.get("successful_payment"):
                await self.handle_successful_payment(message)
                return
            await self.handle_message(message, is_business=False)

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
    def _user_id(message: dict[str, Any]) -> int | None:
        user_id = (message.get("from") or {}).get("id")
        return user_id if isinstance(user_id, int) else None

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        text = message.get("text") or message.get("caption") or ""
        return str(text).strip()

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

    def _effective_system_prompt(self, user_id: int | None = None) -> str:
        # Owner's global role is used for Business automation and owner chat only.
        # A premium user's private AI chat receives only that user's own role.
        role = ""
        if user_id is None or user_id == OWNER_ADMIN_ID:
            role = self.store.get_role("")
        else:
            user_role_method = getattr(self.store, "get_user_role", None)
            if callable(user_role_method):
                role = user_role_method(user_id, "")
        if not role:
            return self.settings.system_prompt
        return f"{self.settings.system_prompt}\n\nQo‘shimcha boshqaruvchi roli:\n{role}"

    def _is_admin(self, message: dict[str, Any]) -> bool:
        sender = message.get("from") or {}
        user_id = sender.get("id")
        return isinstance(user_id, int) and user_id in self.admin_user_ids

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

        if command == "/start":
            sender_id = self._user_id(message)
            if sender_id == OWNER_ADMIN_ID:
                return False
            if sender_id is not None:
                marker = getattr(self.store, "mark_started", None)
                if callable(marker):
                    marker(sender_id)
            await self._send_chunks(chat_id, START_MENU_TEXT, None, reply_to, self._main_menu_keyboard(self._has_premium(sender_id)))
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

        if command in {"/myrole", "/premiumrole"}:
            await self._handle_premium_role(message, argument, chat_id, reply_to)
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
            await self._send_chunks(chat_id, self._admin_panel_text(), None, reply_to, self._admin_panel_keyboard(include_statistics=is_owner, include_main_menu=True))
            return True
        if command not in {"/rol", "/role"}:
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
        if chat_id is None:
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
                LOGGER.warning(
                    "Business botda can_reply huquqi yo‘q: %s; Chat Automation’da reply/send messages huquqini yoqing",
                    business_connection_id,
                )
                return
            if self._is_apk_message(message) and not self._is_admin(message):
                await self._delete_business_apk(message, business_connection_id)
                return

        text = self._message_text(message)
        if not text:
            return
        if not is_business and await self._handle_admin_command(message, text, chat_id):
            return

        user_id = self._user_id(message)
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
        pause_enabled = self._manual_pause_enabled() if is_business else False
        if is_business and self._is_admin(message):
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
            if text.lower() in {"/reset", "/clear"}:
                self.store.clear(storage_key)
                await self._send_chunks(
                    chat_id,
                    "Suhbat tarixi tozalandi.",
                    business_connection_id,
                    message.get("message_id"),
                )
                return

            history = self.store.history(storage_key, self._effective_system_prompt(user_id if not is_business else None))
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
        if data.startswith("admin:") and not (is_owner or is_premium):
            await self.telegram.answer_callback_query(callback_id, "Siz admin emassiz.", True)
            return
        if data == "admin:stats" and not is_owner:
            await self.telegram.answer_callback_query(callback_id, "Bu bo‘lim faqat owner uchun.", True)
            return
        await self.telegram.answer_callback_query(callback_id)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        if data == "menu:home":
            await self.telegram.edit_message_text(chat_id, message_id, START_MENU_TEXT, self._main_menu_keyboard(self._has_premium(user_id)))
            return
        if data == "menu:about":
            await self.telegram.edit_message_text(chat_id, message_id, BOT_ABOUT_TEXT, self._about_keyboard())
            return
        if data == "premium:status":
            await self._edit_premium_panel(chat_id, user_id, message_id)
            return
        if data == "premium:buy":
            await self._send_subscription_offer(chat_id, user_id, None, edit_message_id=message_id)
            return
        if data == "premium:role":
            await self.telegram.edit_message_text(chat_id, message_id, "Shaxsiy AI rolingizni o‘zgartirish uchun /myrole Sizning uslubingiz... buyrug‘ini yuboring.", self._premium_role_keyboard())
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
                    self._set_manual_pause_enabled(not self._manual_pause_enabled())
                enabled = self._manual_pause_enabled()
                state = "YOQILGAN" if enabled else "O‘CHIRILGAN"
                text = f"⏱ Manual pause: {state}\n\nYoqilganda egasi mijozga qo‘lda yozganidan keyin shu chatda AI javobi 30 daqiqaga to‘xtaydi. O‘chirilganda bot 30 daqiqalik qoida bo‘yicha pauza qilmaydi."
                markup = self._admin_pause_keyboard(enabled)
            else:
                text = self._admin_panel_text()
                markup = self._admin_panel_keyboard(include_statistics=is_owner, include_main_menu=True)
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
            if edit_message_id is not None:
                await self._edit_premium_panel(chat_id, user_id, edit_message_id)
            else:
                await self._send_premium_panel(chat_id, user_id, reply_to)
            return
        try:
            link = await self.telegram.create_invoice_link(
                "Premium AI — 1 oy",
                "AI chat, shaxsiy rol va premium funksiyalar. Obuna 30 kun amal qiladi.",
                STAR_SUBSCRIPTION_PAYLOAD,
                STAR_SUBSCRIPTION_AMOUNT,
                STAR_SUBSCRIPTION_PERIOD_SECONDS,
            )
            markup = {"inline_keyboard": [[{"text": "⭐ 100 Stars — obuna bo‘lish", "url": link}]]}
            text = "Premium funksiyalarni ochish uchun oyiga 100 Telegram Stars to‘lang. To‘lov muvaffaqiyatli tasdiqlangach, premium access 30 kunga avtomatik ochiladi."
            if edit_message_id is not None:
                try:
                    await self.telegram.edit_message_text(chat_id, edit_message_id, text, markup)
                except TelegramApiError:
                    await self._send_chunks(chat_id, text, None, None, markup)
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
            text = f"⭐ Premium faol. Qolgan muddat: taxminan {remaining_days} kun.\n\nShaxsiy AI rolingizni /myrole orqali sozlashingiz mumkin."
        else:
            text = "⭐ Premium faol emas. Oylik 100 Stars obunasi bilan AI chat, shaxsiy rol va boshqa premium funksiyalarni oching."
        return text, self._premium_keyboard(active)

    async def _send_premium_panel(self, chat_id: int, user_id: int | None, reply_to: int | None) -> None:
        if user_id is None:
            await self._send_chunks(chat_id, "Premium panelni ochib bo‘lmadi.", None, reply_to)
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

    def _main_menu_keyboard(self, premium_active: bool = False) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "PREMIUM 💎", "callback_data": "premium:status"}],
            [{"text": "Bot haqida 🤖", "callback_data": "menu:about"}],
        ]}

    @staticmethod
    def _about_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "🔙 Asosiy menyu", "callback_data": "menu:home"}]]}

    @staticmethod
    def _premium_role_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "🔙 Premium", "callback_data": "premium:status"}],
            [{"text": "🏠 Asosiy menyu", "callback_data": "menu:home"}],
        ]}

    def _premium_keyboard(self, active: bool) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        if active:
            rows.append([{"text": "🧠 Shaxsiy rol", "callback_data": "premium:role"}])
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

    def _admin_panel_keyboard(self, include_statistics: bool = True, include_main_menu: bool = False) -> dict[str, Any]:
        pause_label = "⏱ Pause: YOQILGAN" if self._manual_pause_enabled() else "⏱ Pause: O‘CHIRILGAN"
        rows: list[list[dict[str, str]]] = []
        if include_statistics:
            rows.append([{"text": "📊 Statistika", "callback_data": "admin:stats"}])
        rows.extend([
            [{"text": "🧠 AI roli", "callback_data": "admin:role"}],
            [{"text": pause_label, "callback_data": "admin:pause"}],
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

    def _manual_pause_enabled(self) -> bool:
        method = getattr(self.store, "manual_pause_enabled", None)
        if not callable(method):
            return True
        try:
            return bool(method(True))
        except Exception:
            LOGGER.exception("Manual pause holatini o‘qib bo‘lmadi")
            return True

    def _set_manual_pause_enabled(self, enabled: bool) -> None:
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
