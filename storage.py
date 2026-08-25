from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


class JsonStore:
    def __init__(self, data_dir: Path, max_history_messages: int = 12):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_history_messages = max_history_messages
        self.path = data_dir / "conversations.json"
        self.lock = Lock()
        self.data: dict[str, list[dict[str, str]]] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"__role__": ""}
        try:
            loaded: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result: dict[str, Any] = {"__role__": str(loaded.get("__role__", ""))}
                result.update({
                    str(key): value
                    for key, value in loaded.items()
                    if key != "__role__" and isinstance(value, list)
                })
                return result
        except (OSError, json.JSONDecodeError):
            # Preserve a broken file for manual inspection instead of destroying it.
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
        return {"__role__": ""}

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def history(self, key: str, system_prompt: str) -> list[dict[str, str]]:
        with self.lock:
            messages = self.data.get(key, [])[-self.max_history_messages :]
        return [{"role": "system", "content": system_prompt}, *messages]

    def append(self, key: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("role faqat user yoki assistant bo‘lishi kerak")
        with self.lock:
            messages = self.data.setdefault(key, [])
            messages.append({"role": role, "content": content})
            self.data[key] = messages[-self.max_history_messages :]
            self._save()

    def clear(self, key: str) -> None:
        with self.lock:
            self.data.pop(key, None)
            self._save()

    def get_role(self, default: str) -> str:
        with self.lock:
            return str(self.data.get("__role__") or default)

    def set_role(self, role: str) -> None:
        with self.lock:
            self.data["__role__"] = role.strip()
            self._save()

    def clear_role(self) -> None:
        with self.lock:
            self.data["__role__"] = ""
            self._save()
