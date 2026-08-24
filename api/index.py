from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from app import BusinessAiBot
from config import Settings
from memory_store import MemoryStore


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("vercel_webhook")


try:
    if not os.getenv("WEBHOOK_SECRET", "").strip():
        raise ValueError("WEBHOOK_SECRET Vercel Environment Variables’da ko‘rsatilmagan")
    settings = Settings.from_env()
    bot = BusinessAiBot(settings, store=MemoryStore(settings.max_history_messages))
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


async def app(scope, receive, send):
    if scope.get("type") != "http":
        return

    path = scope.get("path", "/")
    method = scope.get("method", "GET").upper()

    if method == "GET":
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
