from datetime import UTC, datetime
from decimal import Decimal

from sensex_engine.domain import Contract, OptionLeg, Position, Right, Side, TradingMode
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
