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
            return {"__role__": "", "__manual_pause_enabled__": True, "__settings__": {}, "__user_settings__": {}, "__auto_replies__": {}}
        try:
            loaded: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result: dict[str, Any] = {
                    "__role__": str(loaded.get("__role__", "")),
                    "__owner_activity__": loaded.get("__owner_activity__", {}) if isinstance(loaded.get("__owner_activity__", {}), dict) else {},
                    "__manual_pause_enabled__": loaded.get("__manual_pause_enabled__", True) is not False,
                    "__settings__": loaded.get("__settings__", {}) if isinstance(loaded.get("__settings__", {}), dict) else {},
                    "__user_settings__": loaded.get("__user_settings__", {}) if isinstance(loaded.get("__user_settings__", {}), dict) else {},
                    "__auto_replies__": loaded.get("__auto_replies__", {}) if isinstance(loaded.get("__auto_replies__", {}), dict) else {},
                }
                result.update({
                    str(key): value
                    for key, value in loaded.items()
                    if key not in {"__role__", "__owner_activity__", "__manual_pause_enabled__", "__settings__", "__user_settings__", "__auto_replies__"} and isinstance(value, list)
                })
                return result
        except (OSError, json.JSONDecodeError):
            # Preserve a broken file for manual inspection instead of destroying it.
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
        return {"__role__": "", "__manual_pause_enabled__": True, "__settings__": {}, "__user_settings__": {}, "__auto_replies__": {}}

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

    def get_setting(self, key: str, default: str = "") -> str:
        with self.lock:
            settings = self.data.get("__settings__", {})
            return str(settings.get(key, default)) if isinstance(settings, dict) else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            settings = self.data.setdefault("__settings__", {})
            if isinstance(settings, dict):
                settings[str(key)] = str(value)
            self._save()

    def delete_setting(self, key: str) -> None:
        with self.lock:
            settings = self.data.get("__settings__", {})
            if isinstance(settings, dict):
                settings.pop(str(key), None)
            self._save()

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

    def get_user_setting(self, user_id: int, key: str, default: str = "") -> str:
        with self.lock:
            values = self.data.get("__user_settings__", {})
            if not isinstance(values, dict):
                return default
            user_values = values.get(str(user_id), {})
            return str(user_values.get(str(key), default)) if isinstance(user_values, dict) else default

    def set_user_setting(self, user_id: int, key: str, value: str) -> None:
        with self.lock:
            values = self.data.setdefault("__user_settings__", {})
            if not isinstance(values, dict):
                values = {}
                self.data["__user_settings__"] = values
            user_values = values.setdefault(str(user_id), {})
            if isinstance(user_values, dict):
                user_values[str(key)] = str(value)
            self._save()

    def delete_user_setting(self, user_id: int, key: str) -> None:
        with self.lock:
            values = self.data.get("__user_settings__", {})
            if isinstance(values, dict) and isinstance(values.get(str(user_id)), dict):
                values[str(user_id)].pop(str(key), None)
            self._save()

    def list_auto_replies(self, user_id: int) -> list[dict[str, Any]]:
        with self.lock:
            values = self.data.get("__auto_replies__", {})
            rows = values.get(str(user_id), []) if isinstance(values, dict) else []
            return [dict(row) for row in rows if isinstance(row, dict)]

    def upsert_auto_reply(self, user_id: int, trigger: str, response: str, record_id: int | None = None) -> int:
        trigger = str(trigger).strip()
        response = str(response).strip()
        with self.lock:
            values = self.data.setdefault("__auto_replies__", {})
            if not isinstance(values, dict):
                values = {}
                self.data["__auto_replies__"] = values
            rows = values.setdefault(str(user_id), [])
            if not isinstance(rows, list):
                rows = []
                values[str(user_id)] = rows
            if record_id is not None:
                for row in rows:
                    if isinstance(row, dict) and int(row.get("id", 0)) == int(record_id):
                        row.update({"trigger": trigger, "response": response, "enabled": True})
                        self._save()
                        return int(record_id)
            for row in rows:
                if isinstance(row, dict) and str(row.get("trigger", "")).casefold() == trigger.casefold():
                    row.update({"response": response, "enabled": True})
                    self._save()
                    return int(row.get("id", 0))
            new_id = max([int(row.get("id", 0)) for row in rows if isinstance(row, dict)] + [0]) + 1
            rows.append({"id": new_id, "user_id": int(user_id), "trigger": trigger, "response": response, "enabled": True})
            self._save()
            return new_id

    def get_auto_reply(self, user_id: int, record_id: int) -> dict[str, Any] | None:
        return next((row for row in self.list_auto_replies(user_id) if int(row.get("id", 0)) == int(record_id)), None)

    def delete_auto_reply(self, user_id: int, record_id: int) -> bool:
        with self.lock:
            values = self.data.get("__auto_replies__", {})
            rows = values.get(str(user_id), []) if isinstance(values, dict) else []
            if not isinstance(rows, list):
                return False
            old_len = len(rows)
            values[str(user_id)] = [row for row in rows if not (isinstance(row, dict) and int(row.get("id", 0)) == int(record_id))]
            changed = len(values[str(user_id)]) != old_len
            if changed:
                self._save()
            return changed

    def find_auto_reply(self, user_id: int, trigger: str) -> dict[str, Any] | None:
        wanted = str(trigger).strip().casefold()
        return next((row for row in self.list_auto_replies(user_id) if row.get("enabled", True) and str(row.get("trigger", "")).casefold() == wanted), None)

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
