from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


LOGGER = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig):
        self.config = config

    async def answer(self, messages: list[dict[str, str]]) -> str:
        return await asyncio.to_thread(self._answer_sync, messages)

    def _answer_sync(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 700,
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "telegram-ai-business-bot/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{self.config.name} HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.config.name} tarmoq xatosi: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"{self.config.name} timeout") from exc

        try:
            result: dict[str, Any] = json.loads(raw)
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{self.config.name} yaroqsiz javob qaytardi") from exc

        if isinstance(content, list):
            content = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        answer = str(content).strip()
        if not answer:
            raise ProviderError(f"{self.config.name} bo‘sh javob qaytardi")
        return answer


class AIService:
    def __init__(self, settings: Any):
        self.settings = settings
        self.providers: dict[str, OpenAICompatibleProvider] = {}
        if settings.openai_api_key:
            self.providers["openai"] = OpenAICompatibleProvider(
                ProviderConfig("OpenAI", settings.openai_api_key, settings.openai_base_url, settings.openai_model)
            )
        if settings.qwen_api_key:
            self.providers["qwen"] = OpenAICompatibleProvider(
                ProviderConfig("Qwen", settings.qwen_api_key, settings.qwen_base_url, settings.qwen_model)
            )
        if not self.providers:
            raise ValueError("Hech bo‘lmaganda bitta AI provider sozlanishi kerak")

    def _provider_order(self) -> list[str]:
        selected = self.settings.ai_provider
        if selected in {"openai", "qwen"}:
            return [selected]
        # auto: OpenAI first, then Qwen as a backup when both are configured.
        return [name for name in ("openai", "qwen") if name in self.providers]

    async def answer(self, history: list[dict[str, str]]) -> tuple[str, str]:
        errors: list[str] = []
        for name in self._provider_order():
            try:
                answer = await self.providers[name].answer(history)
                return answer, name
            except ProviderError as exc:
                LOGGER.warning("%s ishlamadi: %s", name, exc)
                errors.append(str(exc))
        raise ProviderError("; ".join(errors))
