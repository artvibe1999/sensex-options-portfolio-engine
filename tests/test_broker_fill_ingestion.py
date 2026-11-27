from decimal import Decimal

import pytest

from sensex_engine.broker_fill_ingestion import (
    BrokerFillEvent,
    BrokerFillIngestionService,
)
from sensex_engine.fill_coordinator import FillCoordinator
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


def setup_ingestion(tmp_path):
    store = SqliteStore(tmp_path / "broker-fills.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    positions.create(
        position_id="position-1",
        strategy_id="BULL_CALL_DEBIT",
        mode="paper",
        idempotency_key="position-create",
        legs=[
            {"leg_index": 0, "instrument_token": 1001, "tradingsymbol": "SENSEX_CE1",
             "side": "BUY", "quantity": 10},
            {"leg_index": 1, "instrument_token": 1002, "tradingsymbol": "SENSEX_CE2",
             "side": "SELL", "quantity": 10},
        ],
    )
    orders.create_intent(
        order_id="order-open-long",
        position_id="position-1",
        leg_index=0,
        instrument_token=1001,
        tradingsymbol="SENSEX_CE1",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        product="MIS",
        mode="paper",
        idempotency_key="order-create",
    )
    orders.transition(
        order_id="order-open-long",
        new_state=OrderState.VALIDATED,
        update_id="validated",
    )
    orders.transition(
        order_id="order-open-long",
        new_state=OrderState.SUBMITTING,
        update_id="submitting",
    )
    orders.transition(
        order_id="order-open-long",
        new_state=OrderState.ACKNOWLEDGED,
        update_id="acknowledged",
        broker_order_id="kite-order-1",
    )
    coordinator = FillCoordinator(orders, positions)
    return BrokerFillIngestionService(coordinator), orders, positions


def event(**overrides):
    values = {
        "broker_order_id": "kite-order-1",
        "broker_trade_id": "trade-1",
        "instrument_token": 1001,
        "tradingsymbol": "SENSEX_CE1",
        "side": "BUY",
        "quantity": 10,
        "price": Decimal("101.50"),
        "filled_at": "2026-11-27T10:10:00+05:30",
        "phase": "OPEN",
    }
    values.update(overrides)
    return BrokerFillEvent(**values)


def test_broker_fill_ingestion_resolves_and_persists_both_ledgers(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)

    result = service.ingest(event())

    assert result.internal_order_id == "order-open-long"
    assert result.broker_order_id == "kite-order-1"
    assert result.order_event_created is True
    assert result.position_event_created is True
    assert orders.snapshot("order-open-long")["filled_quantity"] == 10
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 10


def test_duplicate_broker_trade_event_is_idempotent(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)

    first = service.ingest(event())
    duplicate = service.ingest(event())

    assert first.order_event_created is True
    assert duplicate.order_event_created is False
    assert duplicate.position_event_created is False
    assert orders.snapshot("order-open-long")["filled_quantity"] == 10
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 10


def test_same_trade_id_on_different_broker_orders_has_distinct_fill_key(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)
    service.ingest(event())

    # A second order in the same position can legitimately reuse a broker trade ID
    # in a simulated adapter; the broker-order prefix keeps local event IDs distinct.
    orders.create_intent(
        order_id="order-open-short",
        position_id="position-1",
        leg_index=1,
        instrument_token=1002,
        tradingsymbol="SENSEX_CE2",
        side="SELL",
        quantity=10,
        order_type="LIMIT",
        product="MIS",
        mode="paper",
        idempotency_key="order-create-short",
    )
    for state, update in (
        (OrderState.VALIDATED, "v"),
        (OrderState.SUBMITTING, "s"),
        (OrderState.ACKNOWLEDGED, "a"),
    ):
        orders.transition(
            order_id="order-open-short",
            new_state=state,
            update_id=update,
            broker_order_id="kite-order-2" if state is OrderState.ACKNOWLEDGED else None,
        )

    result = service.ingest(event(
        broker_order_id="kite-order-2",
        instrument_token=1002,
        tradingsymbol="SENSEX_CE2",
        side="SELL",
        phase="OPEN",
    ))
    assert result.order_event_created is True
    assert positions.snapshot("position-1")["legs"][1]["opened_quantity"] == 10


def test_unknown_or_ambiguous_broker_order_id_is_rejected(tmp_path):
    service, _, _ = setup_ingestion(tmp_path)

    with pytest.raises(ValueError, match="exactly one"):
        service.ingest(event(broker_order_id="unknown-order"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"instrument_token": 9999},
        {"tradingsymbol": "OTHER"},
        {"side": "SELL"},
    ],
)
def test_broker_fill_contract_mismatch_is_rejected(tmp_path, overrides):
    service, orders, positions = setup_ingestion(tmp_path)

    with pytest.raises(ValueError, match="does not match"):
        service.ingest(event(**overrides))
    assert orders.snapshot("order-open-long")["filled_quantity"] == 0
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 0},
        {"quantity": True},
        {"price": Decimal("NaN")},
        {"phase": "UNKNOWN"},
        {"broker_trade_id": ""},
    ],
)
def test_malformed_broker_fill_event_is_rejected(tmp_path, overrides):
    service, orders, _ = setup_ingestion(tmp_path)

    with pytest.raises((ValueError, TypeError)):
        service.ingest(event(**overrides))
    assert orders.snapshot("order-open-long")["filled_quantity"] == 0


def test_partial_close_order_quantity_can_be_smaller_than_original_leg(tmp_path):
    store = SqliteStore(tmp_path / "partial-close.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    positions.create(
        position_id="position-1",
        strategy_id="BULL_CALL_DEBIT",
        mode="paper",
        idempotency_key="position-create",
        legs=[
            {"leg_index": 0, "instrument_token": 1001, "tradingsymbol": "SENSEX_CE1",
             "side": "BUY", "quantity": 10},
        ],
    )
    positions.record_leg_fill(
        position_id="position-1", leg_index=0, phase="OPEN", fill_id="open-fill",
        quantity=10, price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )
    positions.transition(position_id="position-1", status="CLOSING", update_id="closing")
    orders.create_intent(
        order_id="partial-close",
        position_id="position-1",
        leg_index=0,
        instrument_token=1001,
        tradingsymbol="SENSEX_CE1",
        side="SELL",
        quantity=4,
        order_type="LIMIT",
        product="MIS",
        mode="paper",
        idempotency_key="partial-close-create",
    )
    for state, update in (
        (OrderState.VALIDATED, "v"),
        (OrderState.SUBMITTING, "s"),
        (OrderState.ACKNOWLEDGED, "a"),
    ):
        orders.transition(order_id="partial-close", new_state=state, update_id=update)
    coordinator = FillCoordinator(orders, positions)

    result = coordinator.record_fill(
        order_id="partial-close", fill_id="close-partial", quantity=4,
        price=Decimal("99"), filled_at="2026-11-27T10:05:00+05:30",
        phase="CLOSE",
    )

    assert result["position"]["legs"][0]["remaining_quantity"] == 6
    assert result["position"]["status"] == "CLOSING"


def test_late_fill_after_cancel_ack_is_ingested_from_terminal_order(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)
    orders.transition(
        order_id="order-open-long",
        new_state=OrderState.CANCEL_PENDING,
        update_id="cancel-pending",
    )
    orders.transition(
        order_id="order-open-long",
        new_state=OrderState.CANCELLED,
        update_id="cancelled",
    )

    result = service.ingest(event(quantity=3, broker_trade_id="late-trade"))

    assert result.order_event_created is True
    assert result.order_state == OrderState.PARTIALLY_FILLED.value
    assert orders.snapshot("order-open-long")["filled_quantity"] == 3
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 3


def test_same_broker_order_id_cannot_be_correlated_to_two_intents(tmp_path):
    service, orders, _ = setup_ingestion(tmp_path)
    orders.create_intent(
        order_id="duplicate-broker-id",
        position_id="position-1",
        leg_index=1,
        instrument_token=1002,
        tradingsymbol="SENSEX_CE2",
        side="SELL",
        quantity=10,
        order_type="LIMIT",
        product="MIS",
        mode="paper",
        idempotency_key="duplicate-broker-id-create",
    )
    for state, update in (
        (OrderState.VALIDATED, "v"),
        (OrderState.SUBMITTING, "s"),
    ):
        orders.transition(
            order_id="duplicate-broker-id",
            new_state=state,
            update_id=update,
        )
    with pytest.raises(ValueError, match="already assigned"):
        orders.transition(
            order_id="duplicate-broker-id",
            new_state=OrderState.ACKNOWLEDGED,
            update_id="ack",
            broker_order_id="kite-order-1",
        )


def test_multiple_partial_fills_complete_one_order_without_overcounting(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)

    first = service.ingest(event(
        broker_trade_id="partial-1",
        quantity=4,
        price=Decimal("100"),
    ))
    second = service.ingest(event(
        broker_trade_id="partial-2",
        quantity=6,
        price=Decimal("102"),
    ))

    assert first.order_state == OrderState.PARTIALLY_FILLED.value
    assert second.order_state == OrderState.FILLED.value
    order = orders.snapshot("order-open-long")
    leg = positions.snapshot("position-1")["legs"][0]
    assert order["filled_quantity"] == 10
    assert leg["opened_quantity"] == 10
    assert leg["open_average_price"] == "101.2"


def test_durable_inbox_recovers_when_process_stops_between_ledgers(tmp_path):
    service, orders, positions = setup_ingestion(tmp_path)
    original = positions.record_leg_fill
    calls = {"count": 0}

    def fail_once(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash after order fill persistence")
        return original(**kwargs)

    positions.record_leg_fill = fail_once
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.ingest(event())

    receipt = orders.store.broker_fill_receipt("kite-order-1:trade-1")
    assert receipt["status"] == "RECEIVED"
    assert orders.snapshot("order-open-long")["filled_quantity"] == 10
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 0

    recovered = service.recover_pending()

    assert len(recovered) == 1
    assert recovered[0].order_event_created is False
    assert recovered[0].position_event_created is True
    assert positions.snapshot("position-1")["legs"][0]["opened_quantity"] == 10
    assert orders.store.broker_fill_receipt("kite-order-1:trade-1")["status"] == "PROCESSED"


def test_duplicate_receipt_with_changed_payload_is_rejected(tmp_path):
    service, orders, _ = setup_ingestion(tmp_path)
    service.ingest(event())

    with pytest.raises(ValueError, match="conflicts with existing payload"):
        service.ingest(event(quantity=9))
