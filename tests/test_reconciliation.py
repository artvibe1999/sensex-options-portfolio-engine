from datetime import UTC, datetime
from decimal import Decimal

from sensex_engine.domain import Contract, OptionLeg, Position, Right, Side, TradingMode
from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.reconciliation import (
    BrokerReconciliationService,
    reconcile_snapshots,
)


def _contract(token: int, symbol: str, strike: str) -> Contract:
    return Contract(
        symbol, token, datetime(2026, 12, 31, tzinfo=UTC),
        Decimal(strike), Right.CE, 20, Decimal("0.05"),
    )


def _position(contracts):
    return Position.create(
        "bull-call-debit",
        (
            OptionLeg(contracts[0], Side.BUY, 1),
            OptionLeg(contracts[1], Side.SELL, 1),
        ),
        TradingMode.PAPER,
    )


def _broker_positions():
    return {
        "net": [
            {"tradingsymbol": "SENSEX80000CE", "quantity": 20},
            {"tradingsymbol": "SENSEX80500CE", "quantity": -20},
        ]
    }


def test_reconciliation_matches_multi_leg_internal_and_broker_quantities():
    contracts = [
        _contract(1, "SENSEX80000CE", "80000"),
        _contract(2, "SENSEX80500CE", "80500"),
    ]
    report = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[_position(contracts)],
        broker_positions=_broker_positions(),
        broker_orders=[{"order_id": "O1", "status": "COMPLETE"}],
    )
    assert report.reconciled
    assert report.expected_quantities == ((1, 20), (2, -20))
    assert report.broker_quantities == ((1, 20), (2, -20))


def test_reconciliation_rejects_quantity_mismatch_and_unknown_position():
    contracts = [
        _contract(1, "SENSEX80000CE", "80000"),
        _contract(2, "SENSEX80500CE", "80500"),
    ]
    broker_positions = _broker_positions()
    broker_positions["net"][0]["quantity"] = 10
    broker_positions["net"].append(
        {"tradingsymbol": "UNMAPPED", "quantity": 5}
    )
    report = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[_position(contracts)],
        broker_positions=broker_positions,
        broker_orders=[],
    )
    assert not report.reconciled
    assert any("quantity mismatch" in item for item in report.mismatches)
    assert any("Unknown non-zero" in item for item in report.mismatches)


def test_reconciliation_rejects_pending_orders_and_malformed_payloads():
    contracts = [_contract(1, "SENSEX80000CE", "80000")]
    pending = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[],
        broker_positions={"net": []},
        broker_orders=[{"order_id": "O2", "status": "TRIGGER PENDING"}],
    )
    assert not pending.reconciled
    assert pending.pending_order_ids == ("O2",)

    malformed = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[],
        broker_positions={},
        broker_orders=[],
    )
    assert not malformed.reconciled
    assert any("missing a net positions list" in item for item in malformed.mismatches)


def test_broker_service_fails_closed_when_session_is_not_authenticated():
    class Broker:
        def is_authenticated(self):
            return False

        def positions(self):
            raise AssertionError("must not fetch unauthenticated broker state")

        def orders(self):
            raise AssertionError("must not fetch unauthenticated broker state")

    report = BrokerReconciliationService().reconcile(
        Broker(), contracts=[], internal_positions=[]
    )
    assert not report.reconciled
    assert "not authenticated" in report.mismatches[0]


def test_reconciliation_rejects_fractional_broker_quantities():
    contracts = [_contract(1, "SENSEX80000CE", "80000")]
    report = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[],
        broker_positions={"net": [
            {"tradingsymbol": "SENSEX80000CE", "quantity": "1.5"}
        ]},
        broker_orders=[],
    )
    assert not report.reconciled
    assert any("invalid net quantity" in item for item in report.mismatches)


def test_reconciliation_blocks_partially_filled_orders_and_error_positions():
    contracts = [
        _contract(1, "SENSEX80000CE", "80000"),
        _contract(2, "SENSEX80500CE", "80500"),
    ]
    position = _position(contracts)
    position.status = "ERROR"
    report = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[position],
        broker_positions=_broker_positions(),
        broker_orders=[{"order_id": "O3", "status": "PARTIALLY FILLED"}],
    )
    assert not report.reconciled
    assert report.pending_order_ids == ("O3",)
    assert any("ERROR state" in item for item in report.mismatches)


def test_reconciliation_ignores_non_bfo_positions_outside_engine_scope():
    contracts = [
        _contract(1, "SENSEX80000CE", "80000"),
        _contract(2, "SENSEX80500CE", "80500"),
    ]
    broker_positions = _broker_positions()
    broker_positions["net"].append({
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "quantity": 100,
    })
    report = reconcile_snapshots(
        contracts=contracts,
        internal_positions=[_position(contracts)],
        broker_positions=broker_positions,
        broker_orders=[],
    )
    assert report.reconciled


def test_reconcile_store_uses_actual_filled_quantities(tmp_path):
    from sensex_engine.persistence import SqliteStore
    from sensex_engine.position_lifecycle import PositionLifecycle

    lifecycle = PositionLifecycle(SqliteStore(tmp_path / "positions.sqlite3"))
    orders = OrderLifecycle(lifecycle.store)
    lifecycle.create(
        position_id="durable-1", strategy_id="BULL_CALL", mode="paper",
        idempotency_key="create-durable",
        legs=[
            {"leg_index": 0, "instrument_token": 1, "tradingsymbol": "SENSEX80000CE",
             "side": "BUY", "quantity": 20},
            {"leg_index": 1, "instrument_token": 2, "tradingsymbol": "SENSEX80500CE",
             "side": "SELL", "quantity": 20},
        ],
    )
    lifecycle.record_leg_fill(
        position_id="durable-1", leg_index=0, phase="OPEN", fill_id="open-a",
        quantity=20, price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )
    lifecycle.record_leg_fill(
        position_id="durable-1", leg_index=1, phase="OPEN", fill_id="open-b",
        quantity=20, price=Decimal("50"), filled_at="2026-11-27T10:00:01+05:30",
    )

    class Broker:
        def is_authenticated(self):
            return True

        def positions(self):
            return {"net": [
                {"exchange": "BFO", "instrument_token": 1, "quantity": 20},
                {"exchange": "BFO", "instrument_token": 2, "quantity": -20},
            ]}

        def orders(self):
            return []

    contracts = [
        _contract(1, "SENSEX80000CE", "80000"),
        _contract(2, "SENSEX80500CE", "80500"),
    ]
    report = BrokerReconciliationService().reconcile_store(
        Broker(), contracts=contracts, position_lifecycle=lifecycle,
        order_lifecycle=orders,
    )
    assert report.reconciled
    assert report.expected_quantities == ((1, 20), (2, -20))


def test_reconcile_store_fails_closed_for_partial_position(tmp_path):
    from sensex_engine.persistence import SqliteStore
    from sensex_engine.position_lifecycle import PositionLifecycle

    lifecycle = PositionLifecycle(SqliteStore(tmp_path / "partial.sqlite3"))
    orders = OrderLifecycle(lifecycle.store)
    lifecycle.create(
        position_id="partial-1", strategy_id="BULL_CALL", mode="paper",
        idempotency_key="create-partial",
        legs=[
            {"leg_index": 0, "instrument_token": 1, "tradingsymbol": "SENSEX80000CE",
             "side": "BUY", "quantity": 20},
            {"leg_index": 1, "instrument_token": 2, "tradingsymbol": "SENSEX80500CE",
             "side": "SELL", "quantity": 20},
        ],
    )
    lifecycle.record_leg_fill(
        position_id="partial-1", leg_index=0, phase="OPEN", fill_id="partial-open",
        quantity=10, price=Decimal("100"), filled_at="2026-11-27T10:00:00+05:30",
    )

    class Broker:
        def is_authenticated(self):
            return True

        def positions(self):
            return {"net": [{"exchange": "BFO", "instrument_token": 1, "quantity": 10}]}

        def orders(self):
            return []

    report = BrokerReconciliationService().reconcile_store(
        Broker(), contracts=[_contract(1, "SENSEX80000CE", "80000"),
                             _contract(2, "SENSEX80500CE", "80500")],
        position_lifecycle=lifecycle, order_lifecycle=orders,
    )
    assert not report.reconciled
    assert any("not fully OPEN" in item for item in report.mismatches)



def test_reconcile_store_blocks_unresolved_live_order_intents(tmp_path):
    from sensex_engine.persistence import SqliteStore
    from sensex_engine.position_lifecycle import PositionLifecycle

    store = SqliteStore(tmp_path / "live-orders.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    orders.create_intent(
        order_id="live-order-1", position_id="position-1", leg_index=0,
        instrument_token=1, tradingsymbol="SENSEX80000CE", side="BUY",
        quantity=20, order_type="LIMIT", product="MIS", mode="live",
        idempotency_key="live-order-create",
    )

    class Broker:
        def is_authenticated(self):
            return True

        def positions(self):
            return {"net": []}

        def orders(self):
            return []

    report = BrokerReconciliationService().reconcile_store(
        Broker(), contracts=[_contract(1, "SENSEX80000CE", "80000")],
        position_lifecycle=positions, order_lifecycle=orders,
    )
    assert not report.reconciled
    assert any(
        "Live internal order live-order-1 requires reconciliation" in issue
        for issue in report.mismatches
    )


def test_reconcile_store_does_not_block_on_paper_order_intents(tmp_path):
    from sensex_engine.persistence import SqliteStore
    from sensex_engine.position_lifecycle import PositionLifecycle

    store = SqliteStore(tmp_path / "paper-orders.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    orders.create_intent(
        order_id="paper-order-1", position_id="position-1", leg_index=0,
        instrument_token=1, tradingsymbol="SENSEX80000CE", side="BUY",
        quantity=20, order_type="LIMIT", product="MIS", mode="paper",
        idempotency_key="paper-order-create",
    )

    class Broker:
        def is_authenticated(self):
            return True

        def positions(self):
            return {"net": []}

        def orders(self):
            return []

    report = BrokerReconciliationService().reconcile_store(
        Broker(), contracts=[_contract(1, "SENSEX80000CE", "80000")],
        position_lifecycle=positions, order_lifecycle=orders,
    )
    assert report.reconciled
