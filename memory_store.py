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
        self.user_pause_enabled: dict[int, bool] = {}
        self.business_profiles: dict[str, dict[str, int | str]] = {}
        self.channels: dict[str, dict[str, str]] = {}
        self.admin_sessions: dict[int, dict[str, object]] = {}
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

    def user_manual_pause_enabled(self, user_id: int, default: bool = True) -> bool:
        with self.lock:
            return self.user_pause_enabled.get(user_id, default)

    def set_user_manual_pause_enabled(self, user_id: int, enabled: bool) -> None:
        with self.lock:
            self.user_pause_enabled[user_id] = bool(enabled)

    def upsert_business_profile(self, connection_id: str, user_id: int) -> None:
        with self.lock:
            profile = self.business_profiles.setdefault(connection_id, {"user_id": user_id, "role": ""})
            profile["user_id"] = user_id

    def get_business_role(self, connection_id: str, default: str = "") -> str:
        with self.lock:
            return str(self.business_profiles.get(connection_id, {}).get("role") or default)

    def set_business_role(self, connection_id: str, role: str) -> None:
        with self.lock:
            self.business_profiles.setdefault(connection_id, {"user_id": 0, "role": ""})["role"] = role.strip()

    def clear_business_role(self, connection_id: str) -> None:
        with self.lock:
            if connection_id in self.business_profiles:
                self.business_profiles[connection_id]["role"] = ""

    def list_vip_users(self) -> list[dict[str, object]]:
        with self.lock:
            now = time.time()
            return [{"user_id": user_id, "premium_until": record[0], "source": record[1]}
                    for user_id, record in self.premium_access.items() if record[0] > now]

    def grant_vip_days(self, user_id: int, days: int) -> None:
        self.grant_premium(user_id, time.time() + max(1, days) * 86400, "owner_grant")

    def revoke_vip(self, user_id: int) -> None:
        with self.lock:
            self.premium_access.pop(user_id, None)

    def list_channels(self) -> list[dict[str, str]]:
        with self.lock:
            return [dict(channel) for channel in self.channels.values()]

    def upsert_channel(self, chat_id: str, title: str = "", username: str = "", channel_type: str = "public", is_required: bool = False, is_main: bool = False, invite_link: str = "", url: str = "") -> None:
        with self.lock:
            self.channels[str(chat_id)] = {
                "chat_id": str(chat_id), "title": title, "username": username,
                "channel_type": channel_type, "is_required": bool(is_required),
                "is_main": bool(is_main), "invite_link": invite_link, "url": url,
            }

    def delete_channel(self, chat_id: str) -> None:
        with self.lock:
            self.channels.pop(str(chat_id), None)

    def required_channels(self) -> list[dict[str, str]]:
        with self.lock:
            return [dict(channel) for channel in self.channels.values() if channel.get("is_required")]

    def broadcast_user_ids(self, target: str = "all") -> list[int]:
        with self.lock:
            users = set(self.started_users)
            if target == "vip":
                users &= set(self.premium_access)
            elif target == "normal":
                users -= set(self.premium_access)
            return sorted(users)

    def get_admin_session(self, user_id: int) -> dict[str, object] | None:
        with self.lock:
            session = self.admin_sessions.get(user_id)
            return dict(session) if session else None

    def set_admin_session(self, user_id: int, state: str, data: dict[str, object] | None = None) -> None:
        with self.lock:
            self.admin_sessions[user_id] = {"state": state, "data": dict(data or {})}

    def clear_admin_session(self, user_id: int) -> None:
        with self.lock:
            self.admin_sessions.pop(user_id, None)

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
