from __future__ import annotations

import time
from threading import Lock


class MemoryStore:
    """Vercel serverless instance uchun diskka yozmaydigan storage.

    Vercel instance’lari almashtirilganda tarix yo‘qolishi mumkin. Doimiy tarix
    kerak bo‘lsa, keyingi bosqichda Redis/Postgres adapteri ulanishi kerak.
    """

    def __init__(self, max_history_messages: int = 12):
        self.max_history_messages = max_history_messages
        self.data: dict[str, list[dict[str, str]]] = {}
        self.role = ""
        self.owner_activity: dict[str, float] = {}
        self.manual_pause_enabled_flag = True
        self.started_users: set[int] = set()
        self.premium_access: dict[int, tuple[float, str]] = {}
        self.star_payments: set[str] = set()
        self.promo_redemptions: set[int] = set()
        self.user_roles: dict[int, str] = {}
        self.lock = Lock()

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

    def clear(self, key: str) -> None:
        with self.lock:
            self.data.pop(key, None)

    def get_role(self, default: str) -> str:
        with self.lock:
            return self.role or default

    def set_role(self, role: str) -> None:
        with self.lock:
            self.role = role.strip()

    def clear_role(self) -> None:
        with self.lock:
            self.role = ""

    def manual_pause_enabled(self, default: bool = True) -> bool:
        with self.lock:
            return self.manual_pause_enabled_flag

    def set_manual_pause_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.manual_pause_enabled_flag = bool(enabled)

    def mark_started(self, user_id: int) -> None:
        with self.lock:
            self.started_users.add(user_id)

    def has_started(self, user_id: int) -> bool:
        with self.lock:
            return user_id in self.started_users

    def has_premium(self, user_id: int) -> bool:
        with self.lock:
            record = self.premium_access.get(user_id)
            return bool(record and record[0] > time.time())

    def premium_until(self, user_id: int) -> float | None:
        with self.lock:
            record = self.premium_access.get(user_id)
            return record[0] if record else None

    def grant_premium(self, user_id: int, premium_until: float, source: str) -> None:
        with self.lock:
            old = self.premium_access.get(user_id)
            if not old or premium_until > old[0]:
                self.premium_access[user_id] = (premium_until, source)

    def record_star_payment(self, **kwargs: Any) -> bool:
        charge_id = str(kwargs.get("charge_id") or "")
        if not charge_id:
            return False
        with self.lock:
            if charge_id in self.star_payments:
                return False
            self.star_payments.add(charge_id)
            return True

    def redeem_promo(self, user_id: int, promo_code: str, expires_at: float) -> bool:
        with self.lock:
            if user_id in self.promo_redemptions:
                return False
            self.promo_redemptions.add(user_id)
            return True

    def get_user_role(self, user_id: int, default: str = "") -> str:
        with self.lock:
            return self.user_roles.get(user_id, default)

    def set_user_role(self, user_id: int, role: str) -> None:
        with self.lock:
            self.user_roles[user_id] = role.strip()

    def clear_user_role(self, user_id: int) -> None:
        with self.lock:
            self.user_roles.pop(user_id, None)

    def mark_owner_activity(self, key: str, timestamp: float | None = None) -> None:
        with self.lock:
            self.owner_activity[key] = timestamp if timestamp is not None else time.time()

    def owner_pause_remaining(self, key: str, pause_seconds: int = 1800) -> int:
        with self.lock:
            last_activity = self.owner_activity.get(key)
        if last_activity is None:
            return 0
        remaining = int(last_activity + pause_seconds - time.time())
        return max(0, remaining)
