import pytest

from sensex_engine.persistence import SqliteStore


def test_event_store_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    store = SqliteStore(path)
    assert store.append_event(
        idempotency_key="intent-001",
        event_type="POSITION_INTENT_CREATED",
        aggregate_id="position-1",
        payload={"lots": 2, "mode": "paper"},
    )
    assert not store.append_event(
        idempotency_key="intent-001",
        event_type="POSITION_INTENT_CREATED",
        aggregate_id="position-1",
        payload={"lots": 2, "mode": "paper"},
    )
    restarted = SqliteStore(path)
    events = restarted.events_for("position-1")
    assert len(events) == 1
    assert events[0]["payload"]["mode"] == "paper"


def test_command_audit_is_idempotent(tmp_path):
    store = SqliteStore(tmp_path / "audit.sqlite3")
    args = {
        "command_id": "update-1",
        "user_id": 100,
        "chat_id": 200,
        "command": "/pause",
        "outcome": "accepted",
    }
    assert store.record_command(**args)
    assert not store.record_command(**args)


def test_event_requires_identity_fields(tmp_path):
    store = SqliteStore(tmp_path / "events.sqlite3")
    with pytest.raises(ValueError):
        store.append_event(
            idempotency_key="",
            event_type="POSITION_OPENED",
            aggregate_id="position-1",
            payload={},
        )


def test_broker_fill_inbox_fences_stale_worker_claims(tmp_path):
    path = tmp_path / "fill-inbox.sqlite3"
    store = SqliteStore(path)
    assert store.receive_broker_fill(
        event_key="order-1:trade-1",
        payload={"quantity": 1, "price": "100"},
    )
    first = store.claim_broker_fill(event_key="order-1:trade-1", lease_seconds=1)
    assert first["attempts"] == 1

    connection = store._connect()
    try:
        connection.execute(
            "UPDATE broker_fill_inbox SET updated_at = '2000-01-01T00:00:00+00:00' "
            "WHERE event_key = ?",
            ("order-1:trade-1",),
        )
        connection.commit()
    finally:
        connection.close()

    second = store.claim_broker_fill(event_key="order-1:trade-1", lease_seconds=1)
    assert second["attempts"] == 2

    with pytest.raises(RuntimeError, match="not claimed"):
        store.fail_broker_fill(
            event_key="order-1:trade-1", attempt=1, error="stale worker failure"
        )
    with pytest.raises(RuntimeError, match="not claimed"):
        store.complete_broker_fill(
            event_key="order-1:trade-1",
            attempt=1,
            result={"status": "stale"},
        )

    store.complete_broker_fill(
        event_key="order-1:trade-1",
        attempt=2,
        result={"status": "processed"},
    )
    assert store.broker_fill_receipt("order-1:trade-1")["status"] == "PROCESSED"



def test_sqlite_health_check_passes_for_initialized_store(tmp_path):
    store = SqliteStore(tmp_path / "healthy.sqlite3")
    assert store.health_check() is True



def test_event_idempotency_rejects_same_key_with_changed_payload(tmp_path):
    store = SqliteStore(tmp_path / "conflicting-events.sqlite3")
    assert store.append_event(
        idempotency_key="account:cash-1",
        event_type="ACCOUNT_CASH_FLOW",
        aggregate_id="account:default",
        payload={"amount": "1000", "direction": "DEPOSIT"},
    )
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        store.append_event(
            idempotency_key="account:cash-1",
            event_type="ACCOUNT_CASH_FLOW",
            aggregate_id="account:default",
            payload={"amount": "2000", "direction": "DEPOSIT"},
        )
