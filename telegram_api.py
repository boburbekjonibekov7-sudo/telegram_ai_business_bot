from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any


LOGGER = logging.getLogger(__name__)


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, description: str, error_code: int | None = None):
        super().__init__(f"Telegram API {method} xatosi: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


class TelegramBotApi:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._call_sync, method, payload or {})

    def _call_sync(self, method: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = max(35, int(payload.get("timeout", 0)) + 15)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise TelegramApiError(method, raw, exc.code) from exc
        except urllib.error.URLError as exc:
            raise TelegramApiError(method, f"Tarmoq xatosi: {exc.reason}") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramApiError(method, "Telegram’dan yaroqsiz JSON javob keldi") from exc
        if not result.get("ok"):
            raise TelegramApiError(method, result.get("description", "Noma’lum xato"), result.get("error_code"))
        return result.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(
        self,
        offset: int | None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "limit": 100}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        return await self.call("getUpdates", payload)

    async def send_typing(
        self,
        chat_id: int,
        business_connection_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        return await self.call("sendChatAction", payload)

    async def get_chat(self, chat_id: int | str) -> dict[str, Any]:
        result = await self.call("getChat", {"chat_id": chat_id})
        if not isinstance(result, dict):
            raise TelegramApiError("getChat", "Telegram chat ma’lumotini qaytarmadi")
        return result

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict[str, Any]:
        result = await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        if not isinstance(result, dict):
            raise TelegramApiError("getChatMember", "Telegram member ma’lumotini qaytarmadi")
        return result

    async def get_chat_join_requests(self, chat_id: int | str, user_id: int | None = None, invite_link: str | None = None, limit: int = 1) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"chat_id": chat_id, "limit": max(1, min(100, limit))}
        if user_id is not None:
            payload["user_id"] = user_id
        if invite_link:
            payload["invite_link"] = invite_link
        result = await self.call("getChatJoinRequests", payload)
        return result if isinstance(result, list) else []

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> Any:
        return await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def delete_business_messages(
        self,
        business_connection_id: str,
        message_ids: list[int],
    ) -> Any:
        return await self.call(
            "deleteBusinessMessages",
            {
                "business_connection_id": business_connection_id,
                "message_ids": message_ids,
            },
        )

    async def create_invoice_link(
        self,
        title: str,
        description: str,
        payload: str,
        amount: int,
        subscription_period: int,
    ) -> str:
        result = await self.call(
            "createInvoiceLink",
            {
                "title": title,
                "description": description,
                "payload": payload,
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": title, "amount": amount}],
                "subscription_period": subscription_period,
            },
        )
        if not isinstance(result, str) or not result:
            raise TelegramApiError("Telegram invoice link qaytarmadi")
        return result

    async def send_invoice(
        self,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        amount: int,
        subscription_period: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invoice: dict[str, Any] = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": title, "amount": amount}],
            "subscription_period": subscription_period,
        }
        if reply_markup is not None:
            invoice["reply_markup"] = reply_markup
        return await self.call("sendInvoice", invoice)

    async def answer_pre_checkout_query(
        self,
        query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"pre_checkout_query_id": query_id, "ok": ok}
        if error_message:
            payload["error_message"] = error_message
        return await self.call("answerPreCheckoutQuery", payload)

    async def edit_user_star_subscription(
        self,
        user_id: int,
        telegram_payment_charge_id: str,
        is_canceled: bool,
    ) -> Any:
        return await self.call(
            "editUserStarSubscription",
            {
                "user_id": user_id,
                "telegram_payment_charge_id": telegram_payment_charge_id,
                "is_canceled": is_canceled,
            },
        )

    async def refund_star_payment(self, user_id: int, telegram_payment_charge_id: str) -> Any:
        return await self.call(
            "refundStarPayment",
            {"user_id": user_id, "telegram_payment_charge_id": telegram_payment_charge_id},
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        return await self.call("answerCallbackQuery", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", payload)

    async def forward_message(
        self,
        chat_id: int | str,
        from_chat_id: int | str,
        message_id: int,
    ) -> dict[str, Any]:
        result = await self.call("forwardMessage", {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        })
        return result if isinstance(result, dict) else {}

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        business_connection_id: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)
