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

    def health_check(self) -> bool:
        """Verify the durable SQLite store is reachable and structurally healthy."""
        with self._connection() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError("SQLite quick_check did not return ok")
            connection.execute("SELECT 1 FROM command_audit LIMIT 1").fetchone()
            connection.execute("SELECT 1 FROM portfolio_events LIMIT 1").fetchone()
        return True

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
                CREATE TABLE IF NOT EXISTS broker_fill_inbox (
                    event_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT event_type, aggregate_id, payload_json FROM portfolio_events "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["event_type"] != event_type
                    or prior["aggregate_id"] != aggregate_id
                    or prior["payload_json"] != encoded
                ):
                    raise ValueError("Event idempotency key conflicts with an existing event")
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO portfolio_events
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
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


    def append_account_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically append account events and enforce a single opening baseline."""
        if not idempotency_key.strip() or not event_type.strip() or not aggregate_id.strip():
            raise ValueError("Account event identity fields are required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT event_type, aggregate_id, payload_json FROM portfolio_events "
                "WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["event_type"] != event_type
                    or prior["aggregate_id"] != aggregate_id
                    or prior["payload_json"] != encoded
                ):
                    raise ValueError("Account event idempotency key conflicts with an existing event")
                connection.commit()
                return False
            if event_type == "ACCOUNT_OPENED":
                exists = connection.execute(
                    "SELECT 1 FROM portfolio_events WHERE aggregate_id = ? "
                    "AND event_type = 'ACCOUNT_OPENED' LIMIT 1",
                    (aggregate_id,),
                ).fetchone()
                if exists is not None:
                    raise ValueError("Opening equity is immutable once the account ledger is opened")
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
            if event_type == "ORDER_STATE_CHANGED" and payload.get("broker_order_id"):
                incoming_broker_id = str(payload["broker_order_id"]).strip()
                if not incoming_broker_id:
                    raise ValueError("Broker order ID must not be blank")
                for event in connection.execute(
                    "SELECT aggregate_id, payload_json FROM portfolio_events "
                    "WHERE event_type = 'ORDER_STATE_CHANGED'"
                ).fetchall():
                    prior_payload = json.loads(event["payload_json"])
                    prior_broker_id = prior_payload.get("broker_order_id")
                    if not prior_broker_id:
                        continue
                    if str(prior_broker_id) == incoming_broker_id:
                        if event["aggregate_id"] != aggregate_id:
                            raise ValueError(
                                "Broker order ID is already assigned to another internal order"
                            )
                existing_ids = {
                    str(json.loads(event["payload_json"])["broker_order_id"])
                    for event in connection.execute(
                        "SELECT payload_json FROM portfolio_events "
                        "WHERE aggregate_id = ? AND event_type = 'ORDER_STATE_CHANGED'",
                        (aggregate_id,),
                    ).fetchall()
                    if json.loads(event["payload_json"]).get("broker_order_id")
                }
                if existing_ids and existing_ids != {incoming_broker_id}:
                    raise ValueError("Internal order broker ID cannot be changed")
            if event_type == "ORDER_INTENT_CREATED":
                if rows:
                    raise ValueError("Order intent already exists")
                incoming_client_key = payload.get("client_order_key")
                if incoming_client_key:
                    existing_intents = connection.execute(
                        "SELECT aggregate_id, payload_json FROM portfolio_events "
                        "WHERE event_type = 'ORDER_INTENT_CREATED'"
                    ).fetchall()
                    for existing in existing_intents:
                        existing_payload = json.loads(existing["payload_json"])
                        if (
                            existing_payload.get("client_order_key") == incoming_client_key
                            and existing["aggregate_id"] != aggregate_id
                        ):
                            raise ValueError(
                                "Client order key is already assigned to another intent"
                            )
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
                    if current_state in {"FILLED", "REJECTED", "EXPIRED"} or (
                        current_state == "CANCELLED"
                        and max_quantity is not None
                        and filled >= max_quantity
                    ):
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
                existing_payload = json.loads(duplicate["payload_json"])
                incoming_payload = dict(payload)
                if event_type == "POSITION_LEG_FILL_RECORDED":
                    existing_payload.pop("status", None)
                if (duplicate["event_type"] != event_type
                    or duplicate["aggregate_id"] != aggregate_id
                    or existing_payload != incoming_payload):
                    raise ValueError(
                        "Position event idempotency key conflicts with existing event"
                    )
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
                    explicit_flat_finalization = (
                        target == "CLOSED" and payload.get("flat_finalization") is True
                    )
                    if target not in allowed.get(status, set()) and not explicit_flat_finalization:
                        raise ValueError(
                            f"Invalid position transition: {status} -> {target}"
                        )
                    if target == "CLOSED" and (
                        not explicit_flat_finalization
                        or any(
                            open_fills.get(index, 0) != close_fills.get(index, 0)
                            for index in legs
                        )
                    ):
                        raise ValueError(
                            "A position can be marked CLOSED only by flat finalization "
                            "when every leg has zero exposure"
                        )
                elif event_type == "POSITION_LEG_FILL_RECORDED":
                    if (
                        fill_phase not in {"OPEN", "CLOSE"}
                        or payload.get("phase") != fill_phase
                        or payload.get("leg_index") != leg_index
                        or payload.get("quantity") != fill_quantity
                        or leg_index not in legs
                        or fill_quantity <= 0
                    ):
                        raise ValueError("Invalid position leg fill parameters")
                    if fill_phase == "OPEN" and status not in {"PENDING", "CLOSING", "CLOSED"}:
                        raise ValueError(
                            "Opening fills are accepted only while pending or reconciling exposure"
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
                        payload["status"] = (
                            "CLOSING" if status in {"CLOSING", "CLOSED"}
                            else "OPEN" if all_open else "PENDING"
                        )
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


    def receive_broker_fill(self, *, event_key: str, payload: dict[str, Any]) -> bool:
        """Durably receipt a broker fill before any portfolio projection is changed."""
        if not event_key.strip():
            raise ValueError("Broker fill event key is required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM broker_fill_inbox WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != encoded:
                    raise ValueError("Broker fill event key conflicts with existing payload")
                return False
            connection.execute(
                """INSERT INTO broker_fill_inbox
                   (event_key, payload_json, status, attempts, received_at, updated_at)
                   VALUES (?, ?, 'RECEIVED', 0, ?, ?)""",
                (event_key, encoded, now, now),
            )
            return True

    def claim_broker_fill(
        self, *, event_key: str, lease_seconds: int = 60
    ) -> dict[str, Any] | None:
        """Claim a receipt; expired PROCESSING claims can be safely replayed."""
        if not event_key.strip() or lease_seconds <= 0:
            raise ValueError("Broker fill claim identity and lease must be valid")
        now = datetime.now(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM broker_fill_inbox WHERE event_key = ?", (event_key,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown broker fill receipt")
            if row["status"] == "PROCESSED":
                connection.commit()
                return None
            if row["status"] == "PROCESSING":
                updated = datetime.fromisoformat(row["updated_at"])
                if (now - updated).total_seconds() < lease_seconds:
                    connection.commit()
                    return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """UPDATE broker_fill_inbox
                   SET status = 'PROCESSING', attempts = ?, updated_at = ?, last_error = ''
                   WHERE event_key = ?""",
                (attempts, now.isoformat(), event_key),
            )
            connection.commit()
            return {
                "event_key": event_key,
                "payload": json.loads(row["payload_json"]),
                "attempts": attempts,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_broker_fill(
        self, *, event_key: str, attempt: int, result: dict[str, Any]
    ) -> None:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0:
            raise ValueError("Broker fill claim attempt must be a positive integer")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE broker_fill_inbox
                   SET status = 'PROCESSED', result_json = ?, last_error = '', updated_at = ?
                   WHERE event_key = ? AND status = 'PROCESSING' AND attempts = ?""",
                (encoded, datetime.now(UTC).isoformat(), event_key, attempt),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broker fill receipt is not claimed for completion")

    def fail_broker_fill(
        self, *, event_key: str, attempt: int, error: str
    ) -> None:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0:
            raise ValueError("Broker fill claim attempt must be a positive integer")
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE broker_fill_inbox
                   SET status = 'RECEIVED', last_error = ?, updated_at = ?
                   WHERE event_key = ? AND status = 'PROCESSING' AND attempts = ?""",
                (error[:500], datetime.now(UTC).isoformat(), event_key, attempt),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broker fill receipt is not claimed for failure")

    def broker_fill_receipt(self, event_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM broker_fill_inbox WHERE event_key = ?", (event_key,)
            ).fetchone()
        if row is None:
            return None
        return {
            "event_key": row["event_key"],
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "last_error": row["last_error"],
        }

    def pending_broker_fills(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Broker fill inbox limit must be between 1 and 1000")
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_key FROM broker_fill_inbox
                   WHERE status != 'PROCESSED' ORDER BY received_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            receipt
            for row in rows
            if (receipt := self.broker_fill_receipt(str(row["event_key"]))) is not None
        ]

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

    def aggregate_ids_for_event(self, event_type: str) -> list[str]:
        """List unique aggregate IDs created by a given event type."""
        if not event_type.strip():
            raise ValueError("Event type is required")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT aggregate_id FROM portfolio_events "
                "WHERE event_type = ? ORDER BY aggregate_id",
                (event_type,),
            ).fetchall()
        return [str(row["aggregate_id"]) for row in rows]

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
