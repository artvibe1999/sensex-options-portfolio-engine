"""Small durable SQLite event/audit store; broker state remains authoritative."""
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SqliteStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def append_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Persist once; return False for a previously recorded idempotency key."""
        if not idempotency_key.strip() or not event_type.strip() or not aggregate_id.strip():
            raise ValueError("Event identity fields are required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO portfolio_events
                    (idempotency_key, event_type, aggregate_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    event_type,
                    aggregate_id,
                    encoded,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def record_command(
        self,
        *,
        command_id: str,
        user_id: int,
        chat_id: int,
        command: str,
        outcome: str,
        detail: str = "",
    ) -> bool:
        if not command_id.strip() or not command.strip():
            raise ValueError("Command identity and text are required")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO command_audit
                    (command_id, user_id, chat_id, command, outcome, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    user_id,
                    chat_id,
                    command,
                    outcome,
                    detail,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def events_for(self, aggregate_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, idempotency_key, event_type, aggregate_id,
                       payload_json, created_at
                FROM portfolio_events
                WHERE aggregate_id = ?
                ORDER BY event_id
                """,
                (aggregate_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "idempotency_key": row["idempotency_key"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


    def update_command_outcome(
        self,
        *,
        command_id: str,
        outcome: str,
        detail: str = "",
    ) -> bool:
        """Finalize a previously persisted command receipt."""
        if not command_id.strip() or not outcome.strip():
            raise ValueError("Command identity and outcome are required")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE command_audit
                SET outcome = ?, detail = ?
                WHERE command_id = ?
                """,
                (outcome, detail[:500], command_id),
            )
            return cursor.rowcount == 1


    def command_audit_for(self, command_id: str) -> dict[str, Any] | None:
        """Return one command audit row for diagnostics."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT command_id, user_id, chat_id, command, outcome, detail, created_at
                FROM command_audit WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
