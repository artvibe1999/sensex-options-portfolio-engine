from decimal import Decimal

import pytest

from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore


def make_ledger(tmp_path):
    store = SqliteStore(tmp_path / "orders.sqlite3")
    ledger = OrderLifecycle(store)
    ledger.create_intent(
        order_id="internal-1",
        position_id="position-1",
        leg_index=0,
        instrument_token=123,
        tradingsymbol="SENSEX_TEST_CE",
        side="BUY",
        quantity=20,
        order_type="LIMIT",
        product="NRML",
        mode="paper",
        idempotency_key="intent-1",
    )
    return ledger


def test_order_lifecycle_persists_transitions_and_fills_after_restart(tmp_path):
    ledger = make_ledger(tmp_path)
    assert ledger.transition(
        order_id="internal-1", new_state=OrderState.VALIDATED, update_id="v1"
    )
    assert ledger.transition(
        order_id="internal-1", new_state=OrderState.SUBMITTING, update_id="s1"
    )
    assert ledger.transition(
        order_id="internal-1", new_state=OrderState.ACKNOWLEDGED,
        update_id="a1", broker_order_id="kite-1",
    )
    assert ledger.record_fill(
        order_id="internal-1", fill_id="fill-1", quantity=8,
        price=Decimal("101.25"), filled_at="2026-11-27T10:00:00+05:30",
    )
    restarted = OrderLifecycle(SqliteStore(tmp_path / "orders.sqlite3"))
    snapshot = restarted.snapshot("internal-1")
    assert snapshot["state"] == "PARTIALLY_FILLED"
    assert snapshot["filled_quantity"] == 8
    assert snapshot["remaining_quantity"] == 12
    assert snapshot["average_fill_price"] == "101.25"
    assert snapshot["broker_order_id"] == "kite-1"


def test_fill_idempotency_and_overfill_protection(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.transition(
        order_id="internal-1", new_state=OrderState.VALIDATED, update_id="v1"
    )
    ledger.transition(
        order_id="internal-1", new_state=OrderState.SUBMITTING, update_id="s1"
    )
    ledger.transition(
        order_id="internal-1", new_state=OrderState.ACKNOWLEDGED, update_id="a1"
    )
    fill = {
        "order_id": "internal-1", "fill_id": "fill-1", "quantity": 10,
        "price": Decimal("100"), "filled_at": "2026-11-27T10:00:00+05:30",
    }
    assert ledger.record_fill(**fill)
    assert not ledger.record_fill(**fill)
    with pytest.raises(ValueError, match="exceed"):
        ledger.record_fill(
            order_id="internal-1", fill_id="fill-2", quantity=11,
            price=Decimal("99"), filled_at="2026-11-27T10:00:01+05:30",
        )


def test_order_lifecycle_rejects_invalid_transition_and_terminal_fill(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(ValueError, match="Invalid order transition"):
        ledger.transition(
            order_id="internal-1", new_state=OrderState.FILLED, update_id="bad"
        )
    ledger.transition(
        order_id="internal-1", new_state=OrderState.REJECTED, update_id="reject"
    )
    with pytest.raises(ValueError, match="terminal"):
        ledger.record_fill(
            order_id="internal-1", fill_id="late-fill", quantity=1,
            price=Decimal("100"), filled_at="2026-11-27T10:00:01+05:30",
        )


def test_order_intent_validates_identity_and_quantity(tmp_path):
    ledger = OrderLifecycle(SqliteStore(tmp_path / "orders.sqlite3"))
    with pytest.raises(ValueError):
        ledger.create_intent(
            order_id="x", position_id="p", leg_index=0, instrument_token=123,
            tradingsymbol="SENSEX_TEST", side="SELL", quantity=0,
            order_type="LIMIT", product="NRML", mode="paper", idempotency_key="x",
        )


def test_replayed_older_fill_remains_idempotent_after_later_fill(tmp_path):
    ledger = make_ledger(tmp_path)
    for state, update_id in (
        (OrderState.VALIDATED, "v1"),
        (OrderState.SUBMITTING, "s1"),
        (OrderState.ACKNOWLEDGED, "a1"),
    ):
        ledger.transition(order_id="internal-1", new_state=state, update_id=update_id)
    first = {
        "order_id": "internal-1", "fill_id": "fill-1", "quantity": 5,
        "price": Decimal("100"), "filled_at": "2026-11-27T10:00:00+05:30",
    }
    assert ledger.record_fill(**first)
    assert ledger.record_fill(
        order_id="internal-1", fill_id="fill-2", quantity=5,
        price=Decimal("102"), filled_at="2026-11-27T10:00:01+05:30",
    )
    assert not ledger.record_fill(**first)
    snapshot = ledger.snapshot("internal-1")
    assert snapshot["filled_quantity"] == 10
    assert snapshot["average_fill_price"] == "101"


def test_fill_idempotency_key_reuse_with_different_data_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.transition(
        order_id="internal-1", new_state=OrderState.VALIDATED, update_id="v1"
    )
    ledger.transition(
        order_id="internal-1", new_state=OrderState.SUBMITTING, update_id="s1"
    )
    ledger.transition(
        order_id="internal-1", new_state=OrderState.ACKNOWLEDGED, update_id="a1"
    )
    ledger.record_fill(
        order_id="internal-1", fill_id="fill-1", quantity=4,
        price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )
    with pytest.raises(ValueError, match="conflicts"):
        ledger.record_fill(
            order_id="internal-1", fill_id="fill-1", quantity=4,
            price=Decimal("101"), filled_at="2026-11-27T10:00:00+05:30",
        )



def test_database_guard_rejects_stale_transition_from_second_instance(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first = make_ledger(tmp_path)
    second = OrderLifecycle(SqliteStore(path))
    assert first.transition(
        order_id="internal-1", new_state=OrderState.VALIDATED, update_id="first"
    )
    with pytest.raises(RuntimeError, match="changed concurrently"):
        second.transition(
            order_id="internal-1", new_state=OrderState.REJECTED, update_id="stale"
        )
    assert second.snapshot("internal-1")["state"] == "VALIDATED"


def test_database_guard_prevents_cross_instance_fill_overrun(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first = make_ledger(tmp_path)
    second = OrderLifecycle(SqliteStore(path))
    for state, update_id in (
        (OrderState.VALIDATED, "v1"),
        (OrderState.SUBMITTING, "s1"),
        (OrderState.ACKNOWLEDGED, "a1"),
    ):
        first.transition(order_id="internal-1", new_state=state, update_id=update_id)
    assert first.record_fill(
        order_id="internal-1", fill_id="fill-a", quantity=15,
        price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )
    with pytest.raises(RuntimeError, match="changed concurrently"):
        second.record_fill(
            order_id="internal-1", fill_id="fill-b", quantity=10,
            price=Decimal("101"), filled_at="2026-11-27T10:00:01+05:30",
        )
    snapshot = second.snapshot("internal-1")
    assert snapshot["filled_quantity"] == 15
    assert snapshot["remaining_quantity"] == 5
