from __future__ import annotations

import logging
import os
import time
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
                        CREATE TABLE IF NOT EXISTS telegram_owner_pauses (
                            business_connection_id TEXT NOT NULL,
                            chat_id BIGINT NOT NULL,
                            owner_last_sent_at DOUBLE PRECISION NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (business_connection_id, chat_id)
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
