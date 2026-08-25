from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator


LOGGER = logging.getLogger("telegram_ai_business_bot.postgres_pause_store")


class PostgresPauseStore:
    """PostgreSQL-backed owner pause storage for stateless deployments."""

    def __init__(self, database_url: str, timeout_seconds: int = 4):
        self.database_url = database_url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "PostgresPauseStore | None":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            return None
        return cls(database_url)

    @staticmethod
    def _parts(key: str) -> tuple[str, int]:
        prefix, separator, rest = key.partition(":")
        if prefix != "business" or not separator:
            raise ValueError("Pause storage key must start with business:")
        connection_id, separator, chat_id_text = rest.rpartition(":")
        if not separator or not connection_id or not chat_id_text:
            raise ValueError("Invalid business pause storage key")
        return connection_id, int(chat_id_text)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - only reached with a bad deployment
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

    def mark_owner_activity(self, key: str, timestamp: float | None = None) -> None:
        connection_id, chat_id = self._parts(key)
        owner_last_sent_at = timestamp if timestamp is not None else time.time()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO telegram_owner_pauses
                            (business_connection_id, chat_id, owner_last_sent_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (business_connection_id, chat_id)
                        DO UPDATE SET owner_last_sent_at = EXCLUDED.owner_last_sent_at,
                                      updated_at = NOW()
                        """,
                        (connection_id, chat_id, owner_last_sent_at),
                    )
        except Exception as exc:
            LOGGER.warning("Postgres pause write failed: %s", exc)

    def owner_pause_remaining(self, key: str, pause_seconds: int = 1800) -> int:
        connection_id, chat_id = self._parts(key)
        try:
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
        except Exception as exc:
            LOGGER.warning("Postgres pause read failed: %s", exc)
            return 0
        if not row:
            return 0
        try:
            last_activity = float(row[0])
        except (TypeError, ValueError):
            return 0
        return max(0, int(last_activity + pause_seconds - time.time()))
