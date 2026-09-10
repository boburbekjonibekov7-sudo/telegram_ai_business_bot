from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
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
            with urllib.request.urlopen(request, timeout=45) as response:
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


class ManusProvider:
    """Manus API v2 agent task provider.

    Manus is asynchronous rather than a normal chat-completions endpoint. This
    adapter creates a short task, polls task.detail, then reads the final
    assistant_message from task.listMessages.
    """

    def __init__(self, api_key: str, base_url: str, agent_profile: str, max_wait_seconds: int = 45):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.agent_profile = agent_profile
        self.max_wait_seconds = max(20, max_wait_seconds)

    async def answer(self, messages: list[dict[str, str]]) -> str:
        return await asyncio.to_thread(self._answer_sync, messages)

    @staticmethod
    def _prompt(messages: list[dict[str, str]]) -> str:
        instructions = ""
        conversation: list[str] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if role == "system" and not instructions:
                instructions = content
            elif role == "user":
                conversation.append(f"Foydalanuvchi: {content}")
            elif role == "assistant":
                conversation.append(f"Yordamchi: {content}")
        sections = []
        if instructions:
            sections.append(f"Ko‘rsatma:\n{instructions}")
        if conversation:
            sections.append("Suhbat:\n" + "\n".join(conversation))
        sections.append("Suhbatdagi oxirgi foydalanuvchi xabariga faqat tayyor javob yozing.")
        return "\n\n".join(sections)

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"Manus HTTP {exc.code}: {detail[:700]}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Manus tarmoq xatosi: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError("Manus timeout") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("Manus yaroqsiz JSON javob qaytardi") from exc
        if not isinstance(result, dict):
            raise ProviderError("Manus javobi object emas")
        if result.get("ok") is False:
            error = result.get("error") or {}
            raise ProviderError(f"Manus {error.get('code', 'error')}: {error.get('message', 'noma’lum xato')}")
        return result

    def _create_task(self, prompt: str) -> str:
        payload = {
            "message": {"content": prompt},
            "agent_profile": self.agent_profile,
            "interactive_mode": False,
            "hide_in_task_list": True,
        }
        request = urllib.request.Request(
            f"{self.base_url}/v2/task.create",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-manus-api-key": self.api_key,
                "User-Agent": "telegram-ai-business-bot/1.0",
            },
            method="POST",
        )
        result = self._request_json(request)
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("Manus task_id qaytarmadi")
        return task_id

    def _task_detail(self, task_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"task_id": task_id})
        request = urllib.request.Request(
            f"{self.base_url}/v2/task.detail?{query}",
            headers={"x-manus-api-key": self.api_key, "User-Agent": "telegram-ai-business-bot/1.0"},
            method="GET",
        )
        return self._request_json(request)

    def _list_messages(self, task_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"task_id": task_id, "limit": 200, "order": "asc"})
        request = urllib.request.Request(
            f"{self.base_url}/v2/task.listMessages?{query}",
            headers={"x-manus-api-key": self.api_key, "User-Agent": "telegram-ai-business-bot/1.0"},
            method="GET",
        )
        return self._request_json(request)

    @staticmethod
    def _assistant_text(messages: list[dict[str, Any]]) -> str:
        for event in reversed(messages):
            if event.get("type") != "assistant_message":
                continue
            content = (event.get("assistant_message") or {}).get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                ).strip()
                if text:
                    return text
        return ""

    def _answer_sync(self, messages: list[dict[str, str]]) -> str:
        task_id = self._create_task(self._prompt(messages))
        deadline = time.monotonic() + self.max_wait_seconds
        status = "running"
        while time.monotonic() < deadline:
            try:
                detail = self._task_detail(task_id)
            except ProviderError as exc:
                # Newly-created v2 tasks can be briefly unavailable to detail.
                if "HTTP 404" not in str(exc):
                    raise
                time.sleep(2)
                continue
            task = detail.get("task") or {}
            status = str(task.get("status", "running"))
            if status in {"stopped", "error", "waiting"}:
                break
            time.sleep(2)

        if status == "waiting":
            raise ProviderError("Manus task foydalanuvchi tasdig‘ini kutmoqda")
        if status == "error":
            raise ProviderError("Manus task xato bilan tugadi")
        if status != "stopped":
            raise ProviderError("Manus javobi timeout bo‘ldi")

        result = self._list_messages(task_id)
        answer = self._assistant_text(result.get("messages") or [])
        if not answer:
            error_events = [
                event.get("error_message", {}).get("content", "")
                for event in result.get("messages") or []
                if event.get("type") == "error_message"
            ]
            detail = next((str(item) for item in reversed(error_events) if item), "bo‘sh javob")
            raise ProviderError(f"Manus javobi olinmadi: {detail[:500]}")
        return answer


class AIService:
    def __init__(self, settings: Any):
        self.settings = settings
        self.providers: dict[str, Any] = {}
        if settings.openai_api_key:
            self.providers["openai"] = OpenAICompatibleProvider(
                ProviderConfig("OpenAI", settings.openai_api_key, settings.openai_base_url, settings.openai_model)
            )
        if settings.qwen_api_key:
            self.providers["qwen"] = OpenAICompatibleProvider(
                ProviderConfig("Qwen", settings.qwen_api_key, settings.qwen_base_url, settings.qwen_model)
            )
        manus_key = getattr(settings, "manus_api_key", "")
        if manus_key:
            self.providers["manus"] = ManusProvider(
                manus_key,
                getattr(settings, "manus_base_url", "https://api.manus.ai"),
                getattr(settings, "manus_agent_profile", "manus-1.6-lite"),
                getattr(settings, "manus_max_wait_seconds", 45),
            )
        if not self.providers:
            raise ValueError("Hech bo‘lmaganda bitta AI provider sozlanishi kerak")

    def _provider_order(self) -> list[str]:
        selected = self.settings.ai_provider
        if selected in {"openai", "qwen", "manus"}:
            return [selected]
        return [name for name in ("openai", "qwen", "manus") if name in self.providers]

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
