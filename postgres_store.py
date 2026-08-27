from __future__ import annotations

import logging
import os
import time
import json
from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator


LOGGER = logging.getLogger("telegram_ai_business_bot.postgres_store")


class PostgresStore:
    """Neon PostgreSQL-backed store for role, chat history and owner pauses."""

    _schema_ready = False
    _schema_lock = Lock()

    def __init__(self, database_url: str, max_history_messages: int = 12, timeout_seconds: int = 4):
        self.database_url = database_url
        self.max_history_messages = max_history_messages
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls, max_history_messages: int = 12) -> "PostgresStore | None":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            return None
        return cls(database_url, max_history_messages)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - deployment packaging error
            raise RuntimeError("psycopg dependency is not installed") from exc
        connection = psycopg.connect(
            self.database_url,
            connect_timeout=self.timeout_seconds,
            autocommit=True,
        )
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        if self.__class__._schema_ready:
            return
        with self.__class__._schema_lock:
            if self.__class__._schema_ready:
                return
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_settings (
                            setting_key TEXT PRIMARY KEY,
                            setting_value TEXT NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_conversations (
                            id BIGSERIAL PRIMARY KEY,
                            business_connection_id TEXT NOT NULL,
                            chat_id BIGINT NOT NULL,
                            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                            content TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS telegram_conversations_chat_idx
                        ON telegram_conversations (business_connection_id, chat_id, id DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_premium_access (
                            user_id BIGINT PRIMARY KEY,
                            premium_until DOUBLE PRECISION NOT NULL,
                            source TEXT NOT NULL,
                            subscription_state TEXT NOT NULL DEFAULT 'active',
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_star_payments (
                            charge_id TEXT PRIMARY KEY,
                            user_id BIGINT NOT NULL,
                            amount INTEGER NOT NULL,
                            currency TEXT NOT NULL,
                            invoice_payload TEXT NOT NULL,
                            subscription_expiration_date DOUBLE PRECISION,
                            is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
                            is_first_recurring BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_promo_redemptions (
                            user_id BIGINT PRIMARY KEY,
                            promo_code TEXT NOT NULL,
                            redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            expires_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_user_starts (
                            user_id BIGINT PRIMARY KEY,
                            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_user_roles (
                            user_id BIGINT PRIMARY KEY,
                            role TEXT NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_owner_pauses (
                            business_connection_id TEXT NOT NULL,
                            chat_id BIGINT NOT NULL,
                            owner_last_sent_at DOUBLE PRECISION NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (business_connection_id, chat_id)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_user_settings (
                            user_id BIGINT PRIMARY KEY,
                            manual_pause_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_business_profiles (
                            business_connection_id TEXT PRIMARY KEY,
                            user_id BIGINT NOT NULL,
                            role TEXT NOT NULL DEFAULT '',
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_vip_channels (
                            chat_id TEXT PRIMARY KEY,
                            title TEXT NOT NULL DEFAULT '',
                            username TEXT NOT NULL DEFAULT '',
                            channel_type TEXT NOT NULL DEFAULT 'public',
                            is_required BOOLEAN NOT NULL DEFAULT FALSE,
                            is_main BOOLEAN NOT NULL DEFAULT FALSE,
                            invite_link TEXT NOT NULL DEFAULT '',
                            url TEXT NOT NULL DEFAULT '',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    for alter_statement in (
                        "ALTER TABLE telegram_vip_channels ADD COLUMN IF NOT EXISTS channel_type TEXT NOT NULL DEFAULT 'public'",
                        "ALTER TABLE telegram_vip_channels ADD COLUMN IF NOT EXISTS is_required BOOLEAN NOT NULL DEFAULT FALSE",
                        "ALTER TABLE telegram_vip_channels ADD COLUMN IF NOT EXISTS is_main BOOLEAN NOT NULL DEFAULT FALSE",
                        "ALTER TABLE telegram_vip_channels ADD COLUMN IF NOT EXISTS invite_link TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE telegram_vip_channels ADD COLUMN IF NOT EXISTS url TEXT NOT NULL DEFAULT ''",
                    ):
                        cursor.execute(alter_statement)
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS telegram_admin_sessions (
                            user_id BIGINT PRIMARY KEY,
                            state TEXT NOT NULL,
                            data JSONB NOT NULL DEFAULT '{}'::jsonb,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
            self.__class__._schema_ready = True

    @staticmethod
    def _parts(key: str) -> tuple[str, int]:
        prefix, separator, rest = key.partition(":")
        if not separator:
            raise ValueError("Invalid Postgres chat storage key")
        if prefix == "normal":
            if not rest:
                raise ValueError("Invalid normal chat storage key")
            return "__normal__", int(rest)
        if prefix != "business":
            raise ValueError("Invalid Postgres chat storage key prefix")
        connection_id, separator, chat_id_text = rest.rpartition(":")
        if not separator or not connection_id or not chat_id_text:
            raise ValueError("Invalid business chat storage key")
        return connection_id, int(chat_id_text)

    def history(self, key: str, system_prompt: str) -> list[dict[str, str]]:
        connection_id, chat_id = self._parts(key)
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT role, content
                        FROM telegram_conversations
                        WHERE business_connection_id = %s AND chat_id = %s
                        ORDER BY id DESC
                        LIMIT %s
                        """,
                        (connection_id, chat_id, self.max_history_messages),
                    )
                    rows = cursor.fetchall()
            messages = [{"role": str(row[0]), "content": str(row[1])} for row in reversed(rows)]
            return [{"role": "system", "content": system_prompt}, *messages]
        except Exception as exc:
            LOGGER.warning("Postgres history read failed: %s", exc)
            return [{"role": "system", "content": system_prompt}]

    def append(self, key: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("role faqat user yoki assistant bo‘lishi kerak")
        connection_id, chat_id = self._parts(key)
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_conversations
                            (business_connection_id, chat_id, role, content)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (connection_id, chat_id, role, content),
                    )
                    cursor.execute(
                        """
                        DELETE FROM telegram_conversations
                        WHERE business_connection_id = %s AND chat_id = %s
                          AND id NOT IN (
                              SELECT id
                              FROM telegram_conversations
                              WHERE business_connection_id = %s AND chat_id = %s
                              ORDER BY id DESC
                              LIMIT %s
                          )
                        """,
                        (connection_id, chat_id, connection_id, chat_id, self.max_history_messages),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres history write failed: %s", exc)

    def clear(self, key: str) -> None:
        connection_id, chat_id = self._parts(key)
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM telegram_conversations WHERE business_connection_id = %s AND chat_id = %s",
                        (connection_id, chat_id),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres history clear failed: %s", exc)

    def get_role(self, default: str) -> str:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT setting_value FROM telegram_settings WHERE setting_key = %s LIMIT 1",
                        ("global_role",),
                    )
                    row = cursor.fetchone()
            return str(row[0]) if row and row[0] else default
        except Exception as exc:
            LOGGER.warning("Postgres role read failed: %s", exc)
            return default

    def set_role(self, role: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_settings (setting_key, setting_value)
                        VALUES (%s, %s)
                        ON CONFLICT (setting_key)
                        DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                        """,
                        ("global_role", role.strip()),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres role write failed: %s", exc)

    def clear_role(self) -> None:
        self.set_role("")

    def manual_pause_enabled(self, default: bool = True) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT setting_value FROM telegram_settings WHERE setting_key = %s LIMIT 1",
                        ("manual_pause_enabled",),
                    )
                    row = cursor.fetchone()
            if not row:
                return default
            return str(row[0]).strip().lower() in {"1", "true", "yes", "on"}
        except Exception as exc:
            LOGGER.warning("Postgres pause setting read failed: %s", exc)
            return default

    def set_manual_pause_enabled(self, enabled: bool) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_settings (setting_key, setting_value)
                        VALUES (%s, %s)
                        ON CONFLICT (setting_key)
                        DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                        """,
                        ("manual_pause_enabled", "1" if enabled else "0"),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres pause setting write failed: %s", exc)

    def mark_started(self, user_id: int) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO telegram_user_starts (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                        (user_id,),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres start marker write failed: %s", exc)

    def has_started(self, user_id: int) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM telegram_user_starts WHERE user_id = %s LIMIT 1", (user_id,))
                    return cursor.fetchone() is not None
        except Exception as exc:
            LOGGER.warning("Postgres start marker read failed: %s", exc)
            return False

    def has_premium(self, user_id: int) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT premium_until FROM telegram_premium_access WHERE user_id = %s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
            return bool(row and float(row[0]) > time.time())
        except Exception as exc:
            LOGGER.warning("Postgres premium read failed: %s", exc)
            return False

    def premium_until(self, user_id: int) -> float | None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT premium_until FROM telegram_premium_access WHERE user_id = %s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
            return float(row[0]) if row else None
        except Exception as exc:
            LOGGER.warning("Postgres premium expiry read failed: %s", exc)
            return None

    def grant_premium(self, user_id: int, premium_until: float, source: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_premium_access (user_id, premium_until, source)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            premium_until = GREATEST(telegram_premium_access.premium_until, EXCLUDED.premium_until),
                            source = EXCLUDED.source,
                            subscription_state = 'active',
                            updated_at = NOW()
                        """,
                        (user_id, premium_until, source),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres premium grant failed: %s", exc)

    def record_star_payment(
        self,
        *,
        charge_id: str,
        user_id: int,
        amount: int,
        currency: str,
        invoice_payload: str,
        subscription_expiration_date: float,
        is_recurring: bool,
        is_first_recurring: bool,
    ) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_star_payments
                            (charge_id, user_id, amount, currency, invoice_payload,
                             subscription_expiration_date, is_recurring, is_first_recurring)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (charge_id) DO NOTHING
                        """,
                        (charge_id, user_id, amount, currency, invoice_payload,
                         subscription_expiration_date, is_recurring, is_first_recurring),
                    )
                    inserted = cursor.rowcount == 1
            return inserted
        except Exception as exc:
            LOGGER.warning("Postgres star payment write failed: %s", exc)
            return False

    def redeem_promo(self, user_id: int, promo_code: str, expires_at: float) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_promo_redemptions (user_id, promo_code, expires_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        (user_id, promo_code, expires_at),
                    )
                    return cursor.rowcount == 1
        except Exception as exc:
            LOGGER.warning("Postgres promo redemption failed: %s", exc)
            return False

    def get_user_role(self, user_id: int, default: str = "") -> str:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT role FROM telegram_user_roles WHERE user_id = %s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
            return str(row[0]) if row and row[0] else default
        except Exception as exc:
            LOGGER.warning("Postgres user role read failed: %s", exc)
            return default

    def set_user_role(self, user_id: int, role: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_user_roles (user_id, role)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, updated_at = NOW()
                        """,
                        (user_id, role.strip()),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres user role write failed: %s", exc)

    def clear_user_role(self, user_id: int) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM telegram_user_roles WHERE user_id = %s", (user_id,))
        except Exception as exc:
            LOGGER.warning("Postgres user role clear failed: %s", exc)

    def premium_count(self) -> int:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM telegram_premium_access WHERE premium_until > %s", (time.time(),))
                    row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            LOGGER.warning("Postgres premium count failed: %s", exc)
            return 0

    def set_subscription_state(self, user_id: int, state: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE telegram_premium_access SET subscription_state = %s, updated_at = NOW() WHERE user_id = %s", (state, user_id))
        except Exception as exc:
            LOGGER.warning("Postgres subscription state update failed: %s", exc)

    def user_manual_pause_enabled(self, user_id: int, default: bool = True) -> bool:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT manual_pause_enabled FROM telegram_user_settings WHERE user_id = %s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
            return bool(row[0]) if row else default
        except Exception as exc:
            LOGGER.warning("Postgres user pause setting read failed: %s", exc)
            return default

    def set_user_manual_pause_enabled(self, user_id: int, enabled: bool) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_user_settings (user_id, manual_pause_enabled)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET manual_pause_enabled = EXCLUDED.manual_pause_enabled, updated_at = NOW()
                        """,
                        (user_id, bool(enabled)),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres user pause setting write failed: %s", exc)

    def upsert_business_profile(self, connection_id: str, user_id: int) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_business_profiles (business_connection_id, user_id)
                        VALUES (%s, %s)
                        ON CONFLICT (business_connection_id) DO UPDATE SET user_id = EXCLUDED.user_id, updated_at = NOW()
                        """,
                        (connection_id, user_id),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres business profile write failed: %s", exc)

    def get_business_role(self, connection_id: str, default: str = "") -> str:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT role FROM telegram_business_profiles WHERE business_connection_id = %s LIMIT 1", (connection_id,))
                    row = cursor.fetchone()
            return str(row[0]) if row and row[0] else default
        except Exception as exc:
            LOGGER.warning("Postgres business role read failed: %s", exc)
            return default

    def set_business_role(self, connection_id: str, role: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_business_profiles (business_connection_id, user_id, role)
                        VALUES (%s, 0, %s)
                        ON CONFLICT (business_connection_id) DO UPDATE SET role = EXCLUDED.role, updated_at = NOW()
                        """,
                        (connection_id, role.strip()),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres business role write failed: %s", exc)

    def clear_business_role(self, connection_id: str) -> None:
        self.set_business_role(connection_id, "")

    def list_vip_users(self) -> list[dict[str, object]]:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT user_id, premium_until, source FROM telegram_premium_access WHERE premium_until > %s ORDER BY premium_until DESC LIMIT 1000", (time.time(),))
                    rows = cursor.fetchall()
            return [{"user_id": int(row[0]), "premium_until": float(row[1]), "source": str(row[2])} for row in rows]
        except Exception as exc:
            LOGGER.warning("Postgres VIP list failed: %s", exc)
            return []

    def grant_vip_days(self, user_id: int, days: int) -> None:
        self.grant_premium(user_id, time.time() + max(1, days) * 86400, "owner_grant")

    def revoke_vip(self, user_id: int) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM telegram_premium_access WHERE user_id = %s", (user_id,))
        except Exception as exc:
            LOGGER.warning("Postgres VIP revoke failed: %s", exc)

    def list_channels(self) -> list[dict[str, str]]:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT chat_id, title, username, channel_type, is_required, is_main, invite_link, url FROM telegram_vip_channels ORDER BY created_at DESC LIMIT 500")
                    rows = cursor.fetchall()
            return [{"chat_id": str(row[0]), "title": str(row[1]), "username": str(row[2]), "channel_type": str(row[3]), "is_required": bool(row[4]), "is_main": bool(row[5]), "invite_link": str(row[6]), "url": str(row[7])} for row in rows]
        except Exception as exc:
            LOGGER.warning("Postgres channel list failed: %s", exc)
            return []

    def upsert_channel(self, chat_id: str, title: str = "", username: str = "", channel_type: str = "public", is_required: bool = False, is_main: bool = False, invite_link: str = "", url: str = "") -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO telegram_vip_channels (chat_id, title, username, channel_type, is_required, is_main, invite_link, url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, username = EXCLUDED.username,
                            channel_type = EXCLUDED.channel_type, is_required = EXCLUDED.is_required,
                            is_main = EXCLUDED.is_main, invite_link = EXCLUDED.invite_link, url = EXCLUDED.url
                    """, (str(chat_id), title, username, channel_type, bool(is_required), bool(is_main), invite_link, url))
        except Exception as exc:
            LOGGER.warning("Postgres channel write failed: %s", exc)

    def delete_channel(self, chat_id: str) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM telegram_vip_channels WHERE chat_id = %s", (str(chat_id),))
        except Exception as exc:
            LOGGER.warning("Postgres channel delete failed: %s", exc)

    def required_channels(self) -> list[dict[str, str]]:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT chat_id, title, username, channel_type, invite_link, url FROM telegram_vip_channels WHERE is_required = TRUE ORDER BY created_at DESC LIMIT 500")
                    rows = cursor.fetchall()
            return [{"chat_id": str(row[0]), "title": str(row[1]), "username": str(row[2]), "channel_type": str(row[3]), "invite_link": str(row[4]), "url": str(row[5])} for row in rows]
        except Exception as exc:
            LOGGER.warning("Postgres required channel read failed: %s", exc)
            return []

    def broadcast_user_ids(self, target: str = "all") -> list[int]:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    if target == "vip":
                        cursor.execute("""
                            SELECT s.user_id FROM telegram_user_starts s
                            JOIN telegram_premium_access p ON p.user_id = s.user_id
                            WHERE p.premium_until > %s ORDER BY s.user_id LIMIT 5000
                        """, (time.time(),))
                    else:
                        cursor.execute("SELECT user_id FROM telegram_user_starts ORDER BY user_id LIMIT 5000")
                    rows = cursor.fetchall()
            return [int(row[0]) for row in rows]
        except Exception as exc:
            LOGGER.warning("Postgres broadcast recipient read failed: %s", exc)
            return []

    def get_admin_session(self, user_id: int) -> dict[str, object] | None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT state, data FROM telegram_admin_sessions WHERE user_id = %s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
            if not row:
                return None
            data = row[1] if isinstance(row[1], dict) else json.loads(str(row[1]))
            return {"state": str(row[0]), "data": data}
        except Exception as exc:
            LOGGER.warning("Postgres admin session read failed: %s", exc)
            return None

    def set_admin_session(self, user_id: int, state: str, data: dict[str, object] | None = None) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO telegram_admin_sessions (user_id, state, data) VALUES (%s, %s, %s::jsonb)
                        ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, data = EXCLUDED.data, updated_at = NOW()
                    """, (user_id, state, json.dumps(data or {}, ensure_ascii=False)))
        except Exception as exc:
            LOGGER.warning("Postgres admin session write failed: %s", exc)

    def clear_admin_session(self, user_id: int) -> None:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM telegram_admin_sessions WHERE user_id = %s", (user_id,))
        except Exception as exc:
            LOGGER.warning("Postgres admin session clear failed: %s", exc)

    def mark_owner_activity(self, key: str, timestamp: float | None = None) -> None:
        connection_id, chat_id = self._parts(key)
        owner_last_sent_at = timestamp if timestamp is not None else time.time()
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_owner_pauses
                            (business_connection_id, chat_id, owner_last_sent_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (business_connection_id, chat_id)
                        DO UPDATE SET owner_last_sent_at = EXCLUDED.owner_last_sent_at, updated_at = NOW()
                        """,
                        (connection_id, chat_id, owner_last_sent_at),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres pause write failed: %s", exc)

    def owner_pause_remaining(self, key: str, pause_seconds: int = 1800) -> int:
        connection_id, chat_id = self._parts(key)
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT owner_last_sent_at
                        FROM telegram_owner_pauses
                        WHERE business_connection_id = %s AND chat_id = %s
                        LIMIT 1
                        """,
                        (connection_id, chat_id),
                    )
                    row = cursor.fetchone()
            if not row:
                return 0
            return max(0, int(float(row[0]) + pause_seconds - time.time()))
        except Exception as exc:
            LOGGER.warning("Postgres pause read failed: %s", exc)
            return 0

    def conversation_count(self) -> int:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(DISTINCT (business_connection_id, chat_id)) FROM telegram_conversations")
                    row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            LOGGER.warning("Postgres conversation count failed: %s", exc)
            return 0

    def pause_count(self) -> int:
        try:
            self._ensure_schema()
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM telegram_owner_pauses")
                    row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            LOGGER.warning("Postgres pause count failed: %s", exc)
            return 0
