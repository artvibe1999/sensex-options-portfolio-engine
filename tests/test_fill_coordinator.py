from decimal import Decimal

import pytest

from sensex_engine.fill_coordinator import FillCoordinator
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


def setup_pair(tmp_path):
    store = SqliteStore(tmp_path / "portfolio.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    positions.create(
        position_id="p-1", strategy_id="BULL_CALL_DEBIT", mode="paper",
        idempotency_key="position-create",
        legs=[
            {"leg_index": 0, "instrument_token": 1001, "tradingsymbol": "SENSEXCE1",
             "side": "BUY", "quantity": 10},
            {"leg_index": 1, "instrument_token": 1002, "tradingsymbol": "SENSEXCE2",
             "side": "SELL", "quantity": 10},
        ],
    )
    orders.create_intent(
        order_id="o-1", position_id="p-1", leg_index=0, instrument_token=1001,
        tradingsymbol="SENSEXCE1", side="BUY", quantity=10, order_type="LIMIT",
        product="MIS", mode="paper", idempotency_key="order-create",
    )
    for state, update in (
        (OrderState.VALIDATED, "v"),
        (OrderState.SUBMITTING, "s"),
        (OrderState.ACKNOWLEDGED, "a"),
    ):
        orders.transition(order_id="o-1", new_state=state, update_id=update)
    return FillCoordinator(orders, positions), orders, positions


def test_fill_coordinator_updates_order_and_position(tmp_path):
    coordinator, orders, positions = setup_pair(tmp_path)
    result = coordinator.record_fill(
        order_id="o-1", fill_id="broker-fill-1", quantity=10,
        price=Decimal("101.25"), filled_at="2026-11-27T10:00:00+05:30",
        phase="OPEN",
    )
    assert result["order_event_created"] is True
    assert result["position_event_created"] is True
    assert orders.snapshot("o-1")["filled_quantity"] == 10
    assert positions.snapshot("p-1")["legs"][0]["opened_quantity"] == 10


def test_fill_coordinator_replay_repairs_interrupted_position_projection(tmp_path):
    coordinator, orders, positions = setup_pair(tmp_path)
    orders.record_fill(
        order_id="o-1", fill_id="broker-fill-2", quantity=10,
        price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )
    result = coordinator.record_fill(
        order_id="o-1", fill_id="broker-fill-2", quantity=10,
        price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
        phase="OPEN",
    )
    assert result["order_event_created"] is False
    assert result["position_event_created"] is True
    assert positions.snapshot("p-1")["legs"][0]["opened_quantity"] == 10


def test_fill_coordinator_rejects_wrong_side_or_contract(tmp_path):
    coordinator, orders, _ = setup_pair(tmp_path)
    with pytest.raises(ValueError, match="Order side"):
        coordinator.record_fill(
            order_id="o-1", fill_id="bad", quantity=10,
            price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
            phase="CLOSE",
        )


def test_fill_coordinator_replay_is_idempotent(tmp_path):
    coordinator, _, positions = setup_pair(tmp_path)
    args = dict(
        order_id="o-1", fill_id="broker-fill-3", quantity=10,
        price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
        phase="OPEN",
    )
    assert coordinator.record_fill(**args)["position_event_created"] is True
    result = coordinator.record_fill(**args)
    assert result["order_event_created"] is False
    assert result["position_event_created"] is False
    assert positions.snapshot("p-1")["legs"][0]["opened_quantity"] == 10
