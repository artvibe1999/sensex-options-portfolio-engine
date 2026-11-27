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


    def append_order_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        expected_state: str | None = None,
        max_quantity: int | None = None,
        fill_quantity: int = 0,
    ) -> bool:
        """Atomically validate and append an order event across all store instances."""
        if not idempotency_key.strip() or not event_type.strip() or not aggregate_id.strip():
            raise ValueError("Event identity fields are required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT event_type, aggregate_id, payload_json FROM portfolio_events "
                "WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if duplicate is not None:
                if (duplicate["event_type"] != event_type
                    or duplicate["aggregate_id"] != aggregate_id
                    or duplicate["payload_json"] != encoded):
                    raise ValueError("Order event idempotency key conflicts with existing event")
                connection.commit()
                return False
            rows = connection.execute(
                "SELECT event_type, payload_json FROM portfolio_events "
                "WHERE aggregate_id = ? ORDER BY event_id", (aggregate_id,)
            ).fetchall()
            if event_type == "ORDER_INTENT_CREATED":
                if rows:
                    raise ValueError("Order intent already exists")
            else:
                if not rows or rows[0]["event_type"] != "ORDER_INTENT_CREATED":
                    raise ValueError("Unknown order intent")
                current_state = rows[0] and json.loads(rows[0]["payload_json"])["state"]
                filled = 0
                for row in rows[1:]:
                    prior = json.loads(row["payload_json"])
                    if row["event_type"] in {"ORDER_STATE_CHANGED", "ORDER_FILL_RECORDED"}:
                        current_state = prior["state"]
                    if row["event_type"] == "ORDER_FILL_RECORDED":
                        filled += int(prior["quantity"])
                if expected_state is not None and current_state != expected_state:
                    raise RuntimeError("Order state changed concurrently; reload before retrying")
                if fill_quantity:
                    if current_state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}:
                        raise ValueError("Cannot add a fill to a terminal order")
                    if max_quantity is None or filled + fill_quantity > max_quantity:
                        raise ValueError("Cumulative fills exceed requested order quantity")
            connection.execute(
                "INSERT INTO portfolio_events "
                "(idempotency_key, event_type, aggregate_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, event_type, aggregate_id, encoded, datetime.now(UTC).isoformat()),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


    def append_position_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        expected_status: str | None = None,
        fill_phase: str | None = None,
        leg_index: int | None = None,
        fill_quantity: int = 0,
    ) -> bool:
        """Serialize position creation, status changes and leg fills transactionally."""
        if not idempotency_key.strip() or not event_type.strip() or not aggregate_id.strip():
            raise ValueError("Position event identity fields are required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT event_type, aggregate_id, payload_json FROM portfolio_events "
                "WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if duplicate is not None:
                if (duplicate["event_type"] != event_type
                    or duplicate["aggregate_id"] != aggregate_id
                    or duplicate["payload_json"] != encoded):
                    raise ValueError("Position event idempotency key conflicts with existing event")
                connection.commit()
                return False
            rows = connection.execute(
                "SELECT event_type, payload_json FROM portfolio_events "
                "WHERE aggregate_id = ? ORDER BY event_id", (aggregate_id,)
            ).fetchall()
            if event_type == "POSITION_CREATED":
                if rows:
                    raise ValueError("Position already exists")
                if payload.get("status") != "PENDING" or not payload.get("legs"):
                    raise ValueError("Position must start PENDING with at least one leg")
            else:
                if not rows or rows[0]["event_type"] != "POSITION_CREATED":
                    raise ValueError("Unknown position")
                created = json.loads(rows[0]["payload_json"])
                status = created["status"]
                legs = {int(leg["leg_index"]): leg for leg in created["legs"]}
                open_fills: dict[int, int] = {}
                close_fills: dict[int, int] = {}
                for row in rows[1:]:
                    prior = json.loads(row["payload_json"])
                    if row["event_type"] == "POSITION_STATUS_CHANGED":
                        status = prior["status"]
                    elif row["event_type"] == "POSITION_LEG_FILL_RECORDED":
                        target = open_fills if prior["phase"] == "OPEN" else close_fills
                        index = int(prior["leg_index"])
                        target[index] = target.get(index, 0) + int(prior["quantity"])
                        if prior.get("status"):
                            status = prior["status"]
                if expected_status is not None and status != expected_status:
                    raise RuntimeError(
                        "Position status changed concurrently; reload before retrying"
                    )
                if event_type == "POSITION_STATUS_CHANGED":
                    target = payload.get("status")
                    allowed = {
                        "PENDING": {"CLOSING", "ERROR"},
                        "OPEN": {"CLOSING", "ERROR"},
                        "CLOSING": {"ERROR"},
                        "ERROR": {"CLOSING"},
                        "CLOSED": set(),
                    }
                    if target not in allowed.get(status, set()):
                        raise ValueError(
                            f"Invalid position transition: {status} -> {target}"
                        )
                elif event_type == "POSITION_LEG_FILL_RECORDED":
                    if fill_phase not in {"OPEN", "CLOSE"} or leg_index not in legs or fill_quantity <= 0:
                        raise ValueError("Invalid position leg fill parameters")
                    if fill_phase == "OPEN" and status != "PENDING":
                        raise ValueError(
                            "Opening fills are accepted only while position is PENDING"
                        )
                    if fill_phase == "CLOSE" and status != "CLOSING":
                        raise ValueError(
                            "Closing fills are accepted only while position is CLOSING"
                        )
                    opened = open_fills.get(leg_index, 0)
                    closed = close_fills.get(leg_index, 0)
                    limit = int(legs[leg_index]["quantity"])
                    if fill_phase == "OPEN" and opened + fill_quantity > limit:
                        raise ValueError("Opening fills exceed planned leg quantity")
                    if fill_phase == "CLOSE" and closed + fill_quantity > opened:
                        raise ValueError("Closing fills exceed confirmed open leg quantity")
                    if fill_phase == "OPEN":
                        all_open = all(
                            open_fills.get(index, 0) + (fill_quantity if index == leg_index else 0)
                            == int(leg["quantity"]) for index, leg in legs.items()
                        )
                        payload["status"] = "OPEN" if all_open else "PENDING"
                    else:
                        all_closed = all(
                            close_fills.get(index, 0) + (fill_quantity if index == leg_index else 0)
                            == open_fills.get(index, 0) for index in legs
                        )
                        payload["status"] = "CLOSED" if all_closed else "CLOSING"
                else:
                    raise ValueError("Unsupported position event type")
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            connection.execute(
                "INSERT INTO portfolio_events "
                "(idempotency_key, event_type, aggregate_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, event_type, aggregate_id, encoded, datetime.now(UTC).isoformat()),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
