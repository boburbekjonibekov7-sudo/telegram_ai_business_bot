from __future__ import annotations

import asyncio
import os
import sys
import unittest


os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("BOT_TOKEN", "123456789:TEST")
os.environ.setdefault("OPENAI_API_KEY", "test-openai")
os.environ.setdefault("AI_PROVIDER", "openai")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api import index  # noqa: E402


class VercelEndpointTests(unittest.TestCase):
    def run_asgi(self, scope, messages):
        sent = []
        receive_messages = iter(messages)

        async def receive():
            return next(receive_messages)

        async def send(message):
            sent.append(message)

        asyncio.run(index.app(scope, receive, send))
        return sent

    def test_health_check(self) -> None:
        sent = self.run_asgi({"type": "http", "method": "GET", "path": "/"}, [])
        self.assertEqual(sent[0]["status"], 200)
        self.assertIn(b"webhook is running", sent[1]["body"])

    def test_wrong_path_is_rejected(self) -> None:
        sent = self.run_asgi(
            {"type": "http", "method": "POST", "path": "/webhook/wrong", "headers": []},
            [{"type": "http.request", "body": b"{}", "more_body": False}],
        )
        self.assertEqual(sent[0]["status"], 404)

    def test_wrong_secret_is_rejected(self) -> None:
        sent = self.run_asgi(
            {"type": "http", "method": "POST", "path": "/webhook/test-secret", "headers": []},
            [{"type": "http.request", "body": b"{}", "more_body": False}],
        )
        self.assertEqual(sent[0]["status"], 401)


if __name__ == "__main__":
    unittest.main()
