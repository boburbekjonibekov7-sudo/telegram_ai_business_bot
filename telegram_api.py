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

    async def send_message(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str | None = None,
        reply_to_message_id: int | None = None,
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
        return await self.call("sendMessage", payload)
