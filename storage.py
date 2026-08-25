from __future__ import annotations

import json
import os
import time
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
            return {"__role__": "", "__manual_pause_enabled__": True}
        try:
            loaded: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result: dict[str, Any] = {
                    "__role__": str(loaded.get("__role__", "")),
                    "__owner_activity__": loaded.get("__owner_activity__", {}) if isinstance(loaded.get("__owner_activity__", {}), dict) else {},
                    "__manual_pause_enabled__": loaded.get("__manual_pause_enabled__", True) is not False,
                }
                result.update({
                    str(key): value
                    for key, value in loaded.items()
                    if key not in {"__role__", "__owner_activity__", "__manual_pause_enabled__"} and isinstance(value, list)
                })
                return result
        except (OSError, json.JSONDecodeError):
            # Preserve a broken file for manual inspection instead of destroying it.
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
        return {"__role__": "", "__manual_pause_enabled__": True}

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

    def manual_pause_enabled(self, default: bool = True) -> bool:
        with self.lock:
            return bool(self.data.get("__manual_pause_enabled__", default))

    def set_manual_pause_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.data["__manual_pause_enabled__"] = bool(enabled)
            self._save()

    def mark_owner_activity(self, key: str, timestamp: float | None = None) -> None:
        with self.lock:
            activity = self.data.setdefault("__owner_activity__", {})
            if not isinstance(activity, dict):
                activity = {}
                self.data["__owner_activity__"] = activity
            activity[key] = timestamp if timestamp is not None else time.time()
            self._save()

    def owner_pause_remaining(self, key: str, pause_seconds: int = 1800) -> int:
        with self.lock:
            activity = self.data.get("__owner_activity__", {})
            last_activity = activity.get(key) if isinstance(activity, dict) else None
        if not isinstance(last_activity, (int, float)):
            return 0
        return max(0, int(last_activity + pause_seconds - time.time()))
