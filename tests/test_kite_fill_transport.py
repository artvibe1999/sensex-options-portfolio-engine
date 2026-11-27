from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sensex_engine.broker_fill_ingestion import BrokerFillIngestionService
from sensex_engine.fill_coordinator import FillCoordinator
from sensex_engine.kite_fill_transport import KiteTradeHistoryIngestor
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


class FakeKiteTrades:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def order_trades(self, broker_order_id):
        self.calls.append(broker_order_id)
        return self.rows


def setup_live(tmp_path, *, order_side="BUY", order_quantity=10):
    store = SqliteStore(tmp_path / "kite-fill.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    positions.create(
        position_id="position-live",
        strategy_id="BULL_CALL_DEBIT",
        mode="live",
        idempotency_key="position-create",
        legs=[{
            "leg_index": 0,
            "instrument_token": 1001,
            "tradingsymbol": "SENSEX_CE1",
            "side": "BUY",
            "quantity": 10,
        }],
    )
    if order_side == "SELL":
        positions.record_leg_fill(
            position_id="position-live",
            leg_index=0,
            phase="OPEN",
            fill_id="initial-open",
            quantity=10,
            price=Decimal("100"),
            filled_at="2026-11-27T10:00:00+05:30",
        )
        positions.transition(
            position_id="position-live", status="CLOSING", update_id="start-close"
        )
    orders.create_intent(
        order_id="internal-live-order",
        position_id="position-live",
        leg_index=0,
        instrument_token=1001,
        tradingsymbol="SENSEX_CE1",
        side=order_side,
        quantity=order_quantity,
        order_type="LIMIT",
        product="MIS",
        mode="live",
        idempotency_key="order-create",
    )
    for state, update in (
        (OrderState.VALIDATED, "validated"),
        (OrderState.SUBMITTING, "submitting"),
        (OrderState.ACKNOWLEDGED, "acknowledged"),
    ):
        orders.transition(
            order_id="internal-live-order",
            new_state=state,
            update_id=update,
            broker_order_id=(
                "kite-order-1" if state is OrderState.ACKNOWLEDGED else None
            ),
        )
    ingestion = BrokerFillIngestionService(FillCoordinator(orders, positions))
    return orders, positions, ingestion


def trade(**overrides):
    row = {
        "trade_id": "trade-1",
        "order_id": "kite-order-1",
        "exchange": "BFO",
        "tradingsymbol": "SENSEX_CE1",
        "instrument_token": 1001,
        "transaction_type": "BUY",
        "quantity": 10,
        "average_price": 101.25,
        "fill_timestamp": "2026-11-27 10:10:00",
    }
    row.update(overrides)
    return row


def test_kite_trade_history_poll_ingests_individual_trade_and_ist_timestamp(tmp_path):
    orders, positions, ingestion = setup_live(tmp_path)
    client = FakeKiteTrades([trade()])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    results = transport.poll(["internal-live-order"])

    assert len(results) == 1
    assert results[0].broker_trade_id == "trade-1"
    assert results[0].order_state == OrderState.FILLED.value
    assert positions.snapshot("position-live")["legs"][0]["opened_quantity"] == 10
    assert client.calls == ["kite-order-1"]
    assert results[0].position_status == "OPEN"


def test_kite_trade_history_poll_is_idempotent_across_backfill_cycles(tmp_path):
    orders, positions, ingestion = setup_live(tmp_path)
    client = FakeKiteTrades([trade()])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    first = transport.poll(["internal-live-order"])
    second = transport.poll(["internal-live-order"])

    assert first[0].order_event_created is True
    assert second[0].order_event_created is False
    assert orders.snapshot("internal-live-order")["filled_quantity"] == 10
    assert positions.snapshot("position-live")["legs"][0]["opened_quantity"] == 10


def test_kite_trade_history_resolves_close_phase_from_position_leg(tmp_path):
    orders, positions, ingestion = setup_live(tmp_path, order_side="SELL")
    client = FakeKiteTrades([trade(transaction_type="SELL")])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    result = transport.poll(["internal-live-order"])[0]

    leg = positions.snapshot("position-live")["legs"][0]
    assert result.position_status == "CLOSED"
    assert leg["remaining_quantity"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_id": "different-order"},
        {"exchange": "NFO"},
        {"instrument_token": 9999},
        {"tradingsymbol": "OTHER"},
        {"transaction_type": "SELL"},
        {"quantity": True},
        {"average_price": "NaN"},
        {"fill_timestamp": "not-a-time"},
        {"trade_id": ""},
    ],
)
def test_kite_trade_history_rejects_malformed_or_mismatched_rows(tmp_path, overrides):
    orders, positions, ingestion = setup_live(tmp_path)
    client = FakeKiteTrades([trade(**overrides)])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    with pytest.raises(ValueError):
        transport.poll(["internal-live-order"])

    assert orders.snapshot("internal-live-order")["filled_quantity"] == 0
    assert positions.snapshot("position-live")["legs"][0]["opened_quantity"] == 0


def test_kite_trade_history_refuses_paper_intents(tmp_path):
    store = SqliteStore(tmp_path / "paper.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    positions.create(
        position_id="paper-position",
        strategy_id="BULL_CALL_DEBIT",
        mode="paper",
        idempotency_key="paper-position-create",
        legs=[{
            "leg_index": 0, "instrument_token": 1001,
            "tradingsymbol": "SENSEX_CE1", "side": "BUY", "quantity": 10,
        }],
    )
    orders.create_intent(
        order_id="paper-order", position_id="paper-position", leg_index=0,
        instrument_token=1001, tradingsymbol="SENSEX_CE1", side="BUY",
        quantity=10, order_type="LIMIT", product="MIS", mode="paper",
        idempotency_key="paper-order-create",
    )
    ingestion = BrokerFillIngestionService(FillCoordinator(orders, positions))
    client = FakeKiteTrades([trade()])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    with pytest.raises(PermissionError, match="LIVE"):
        transport.poll(["paper-order"])
    assert client.calls == []


def test_kite_trade_history_requires_explicit_current_session_order_ids(tmp_path):
    orders, positions, ingestion = setup_live(tmp_path)
    client = FakeKiteTrades([trade()])
    transport = KiteTradeHistoryIngestor(
        client=client, orders=orders, positions=positions, ingestion=ingestion
    )

    assert transport.poll([]) == []
    assert client.calls == []
    with pytest.raises(ValueError, match="Unknown internal"):
        transport.poll(["not-an-order"])
