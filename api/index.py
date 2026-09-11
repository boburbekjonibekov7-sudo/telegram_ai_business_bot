from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any

from app import BusinessAiBot
from config import Settings
from postgres_store import PostgresStore


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("vercel_webhook")


try:
    if not os.getenv("WEBHOOK_SECRET", "").strip():
        raise ValueError("WEBHOOK_SECRET Vercel Environment Variables’da ko‘rsatilmagan")
    settings = Settings.from_env()
    bot = BusinessAiBot(settings, store=PostgresStore.from_env(settings.max_history_messages))
except Exception:
    # Keep import errors visible in Vercel logs while allowing the function to
    # return a useful configuration error instead of failing silently.
    LOGGER.exception("Vercel function konfiguratsiyasi yuklanmadi")
    settings = None
    bot = None


async def _read_request_body(receive) -> bytes:
    chunks: list[bytes] = []
    total = 0
    max_body_size = 5 * 1024 * 1024
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b""
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > max_body_size:
            raise ValueError("request body is too large")
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _send_response(send, status: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _scope_headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _expected_webhook_path() -> str:
    configured = (os.getenv("WEBHOOK_PATH") or "").strip()
    if configured:
        return "/" + configured.strip("/")
    secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    return f"/webhook/{secret}" if secret else "/webhook"


async def _ensure_telegram_webhook() -> bool:
    """Configure Telegram from Vercel secrets without exposing them to the client."""
    token = (os.getenv("BOT_TOKEN") or "").strip()
    secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if not token or not secret:
        LOGGER.error("Webhook auto-setup skipped: BOT_TOKEN yoki WEBHOOK_SECRET yo‘q")
        return False
    base_url = (os.getenv("PUBLIC_WEBHOOK_BASE_URL") or "https://aichat-boburbekjonibekov7-sudos-projects.vercel.app").rstrip("/")
    webhook_url = f"{base_url}{_expected_webhook_path()}"
    payload = urllib.parse.urlencode({
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": json.dumps(["message", "business_message", "edited_business_message", "deleted_business_messages", "callback_query"]),
    }).encode("utf-8")

    def _request() -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        result = await asyncio.to_thread(_request)
        ok = bool(result.get("ok"))
        LOGGER.info("Telegram webhook auto-setup: ok=%s", ok)
        return ok
    except Exception:
        LOGGER.exception("Telegram webhook auto-setup failed")
        return False


async def app(scope, receive, send):
    if scope.get("type") != "http":
        return

    path = scope.get("path", "/")
    method = scope.get("method", "GET").upper()

    if method == "GET":
        await _ensure_telegram_webhook()
        await _send_response(send, 200, "Telegram AI bot webhook is running")
        return
    if method != "POST":
        await _send_response(send, 405, "Method Not Allowed")
        return

    if settings is None or bot is None:
        await _send_response(send, 500, "Server configuration is incomplete")
        return

    expected_path = _expected_webhook_path()
    if path != expected_path:
        await _send_response(send, 404, "Not Found")
        return

    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if webhook_secret:
        headers = _scope_headers(scope)
        if headers.get("x-telegram-bot-api-secret-token") != webhook_secret:
            await _send_response(send, 401, "Unauthorized")
            return

    try:
        raw_body = await _read_request_body(receive)
        update = json.loads(raw_body.decode("utf-8"))
        if not isinstance(update, dict):
            raise ValueError("Telegram update must be an object")
        await bot.process_update(update)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        LOGGER.warning("Invalid Telegram update: %s", exc)
        await _send_response(send, 400, "Invalid Telegram update")
        return
    except Exception:
        LOGGER.exception("Webhook processing failed")
        await _send_response(send, 500, "Webhook processing failed")
        return

    await _send_response(send, 200, "OK")
