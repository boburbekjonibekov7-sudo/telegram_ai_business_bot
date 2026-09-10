from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request


LOGGER = logging.getLogger("telegram_ai_business_bot.pause_store")


class UpstashPauseStore:
    """Small REST-only adapter for per-chat owner pause timestamps.

    It uses a Redis key with a 30-minute TTL, so it works in stateless Vercel
    instances without adding a Python dependency. Network failures fail open
    and are logged; the bot can still run with its in-memory fallback.
    """

    def __init__(self, url: str, token: str, timeout_seconds: float = 4.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "UpstashPauseStore | None":
        url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
        if not url or not token:
            return None
        return cls(url, token)

    def _request(
        self,
        command: str,
        key: str,
        value: str | None = None,
        query: str | None = None,
    ) -> object | None:
        encoded_key = urllib.parse.quote(key, safe="")
        path = f"{self.url}/{command}/{encoded_key}"
        if value is not None:
            path += f"/{urllib.parse.quote(value, safe='')}"
        if query:
            path += f"?{query}"
        request = urllib.request.Request(
            path,
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("result") if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("Upstash pause storage request failed: %s", exc)
            return None

    def mark_owner_activity(self, key: str, timestamp: float | None = None) -> None:
        value = str(timestamp if timestamp is not None else time.time())
        # EX 1800 prevents stale pause keys from accumulating.
        self._request("set", key, value, query="EX=1800")

    def owner_pause_remaining(self, key: str, pause_seconds: int = 1800) -> int:
        raw = self._request("get", key)
        try:
            last_activity = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            last_activity = None
        if last_activity is None:
            return 0
        return max(0, int(last_activity + pause_seconds - time.time()))
