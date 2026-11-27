from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sensex_engine.broker_fill_ingestion import (
    BrokerFillEvent,
    BrokerFillIngestionService,
)
from sensex_engine.broker_submission import (
    BrokerOrderReceipt,
    BrokerOrderStatus,
    DurableBrokerSubmissionService,
)
from sensex_engine.domain import Contract, OptionLeg, Position, Right, Side, TradingMode
from sensex_engine.execution import ExecutionPhase
from sensex_engine.fill_coordinator import FillCoordinator
from sensex_engine.live_execution import LiveMultiLegExecutionCoordinator
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


def make_position():
    expiry = datetime.now(UTC)
    long = Contract("SENSEX_LONG", 101, expiry, Decimal("80000"), Right.CE, 10, Decimal("0.05"))
    short = Contract("SENSEX_SHORT", 102, expiry, Decimal("80200"), Right.CE, 10, Decimal("0.05"))
    return Position.create(
        "BULL_CALL_DEBIT",
        (OptionLeg(short, Side.SELL, 1), OptionLeg(long, Side.BUY, 1)),
        TradingMode.LIVE,
    )


class FakeBroker:
    def __init__(self, order_plans):
        self.order_plans = list(order_plans)
        self.orders = {}
        self.key_to_id = {}
        self.submit_calls = []
        self.cancel_calls = []

    def submit_order(self, action, *, client_order_key):
        self.submit_calls.append((action, client_order_key))
        plan = self.order_plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        order_id = f"kite-{len(self.submit_calls)}"
        self.key_to_id[client_order_key] = order_id
        self.orders[order_id] = {
            "status": plan.get("status", BrokerOrderStatus.OPEN),
            "fills": plan.get("fills", []),
        }
        return BrokerOrderReceipt(order_id, BrokerOrderStatus.ACCEPTED)

    def find_orders_by_client_key(self, client_order_key, *, expected_action=None):
        order_id = self.key_to_id.get(client_order_key)
        if order_id is None:
            return ()
        return (BrokerOrderReceipt(order_id, self.orders[order_id]["status"]),)

    def cancel_order(self, broker_order_id, *, client_order_key):
        self.cancel_calls.append((broker_order_id, client_order_key))
        order = self.orders[broker_order_id]
        if order["status"] not in {BrokerOrderStatus.OPEN, BrokerOrderStatus.PARTIALLY_FILLED}:
            raise RuntimeError("order is no longer cancellable")
        order["status"] = BrokerOrderStatus.CANCELLED
        return BrokerOrderReceipt(broker_order_id, BrokerOrderStatus.OPEN, "cancel requested")


class FakeTradeHistory:
    def __init__(self, broker, orders, positions):
        self.broker = broker
        self.orders = orders
        self.positions = positions
        self.poll_calls = []

    def poll(self, order_ids):
        self.poll_calls.extend(order_ids)
        for internal_id in order_ids:
            order = self.orders.snapshot(internal_id)
            if not order or not order.get("broker_order_id"):
                continue
            broker_order = self.broker.orders[order["broker_order_id"]]
            position = self.positions.snapshot(order["position_id"])
            leg = next(x for x in position["legs"] if x["leg_index"] == order["leg_index"])
            phase = "OPEN" if order["side"] == leg["side"] else "CLOSE"
            for index, (quantity, price) in enumerate(broker_order["fills"]):
                self._ingest(
                    order, phase, f"trade-{index}", quantity, price
                )

    def _ingest(self, order, phase, trade_id, quantity, price):
        # Route through the same durable inbox/coordinator as the production transport.
        event = BrokerFillEvent(
            broker_order_id=order["broker_order_id"],
            broker_trade_id=trade_id,
            instrument_token=order["instrument_token"],
            tradingsymbol=order["tradingsymbol"],
            side=order["side"],
            quantity=quantity,
            price=Decimal(price),
            filled_at="2026-11-27T10:00:00+05:30",
            phase=phase,
        )
        self._ingestion.ingest(event)

    def bind_ingestion(self, ingestion):
        self._ingestion = ingestion

    def recover_pending(self):
        return self._ingestion.recover_pending()

    def has_pending_receipts(self):
        return self._ingestion.has_pending_receipts()


def build_coordinator(tmp_path, plans, *, max_close_replacements=1):
    store = SqliteStore(tmp_path / "live-execution.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    broker = FakeBroker(plans)
    submission = DurableBrokerSubmissionService(
        orders, broker, live_submission_enabled=True
    )
    history = FakeTradeHistory(broker, orders, positions)
    ingestion = BrokerFillIngestionService(FillCoordinator(orders, positions))
    history.bind_ingestion(ingestion)
    coordinator = LiveMultiLegExecutionCoordinator(
        orders=orders,
        positions=positions,
        submission=submission,
        trade_history=history,
        limit_price=lambda action: Decimal("100.00"),
        live_execution_enabled=True,
        max_close_replacements=max_close_replacements,
    )
    return coordinator, orders, positions, broker, history


def test_live_open_submits_protective_long_then_short_only_after_fills(tmp_path):
    coordinator, orders, positions, broker, history = build_coordinator(
        tmp_path,
        [
            {"fills": [(10, "100.00")]},
            {"fills": [(10, "101.00")]},
        ],
    )
    position = make_position()
    report = coordinator.execute(position, ExecutionPhase.OPEN)

    assert report.completed and not report.blocked
    assert [call[0].side for call in broker.submit_calls] == ["BUY", "SELL"]
    snapshot = positions.snapshot(str(position.position_id))
    assert snapshot["status"] == "OPEN"
    assert snapshot["is_fully_open"]
    assert all(item["state"] == OrderState.FILLED.value for item in orders.all_snapshots())


def test_partial_second_leg_is_cancelled_then_residual_is_flattened_short_first(tmp_path):
    coordinator, orders, positions, broker, history = build_coordinator(
        tmp_path,
        [
            {"fills": [(10, "100.00")]},
            {"fills": [(5, "101.00")]},
            {"fills": [(5, "99.00")]},
            {"fills": [(10, "98.00")]},
        ],
    )
    position = make_position()
    report = coordinator.execute(position, ExecutionPhase.OPEN)

    assert not report.completed
    assert not report.blocked, (report, orders.all_snapshots(), broker.cancel_calls)
    assert report.residual_exposure == ()
    assert [call[0].side for call in broker.submit_calls] == ["BUY", "SELL", "BUY", "SELL"]
    assert len(broker.cancel_calls) == 1
    snapshot = positions.snapshot(str(position.position_id))
    assert snapshot["status"] == "CLOSED"
    assert snapshot["is_fully_closed"]


def test_uncertain_submit_is_never_retried_and_blocks_later_legs(tmp_path):
    coordinator, orders, positions, broker, _ = build_coordinator(
        tmp_path, [TimeoutError("network timeout")]
    )
    position = make_position()
    report = coordinator.execute(position, ExecutionPhase.OPEN)

    assert report.blocked
    assert len(broker.submit_calls) == 1
    assert not any(call[0].side == "SELL" for call in broker.submit_calls)
    assert positions.snapshot(str(position.position_id))["status"] == "ERROR"
    repeated = coordinator.execute(position, ExecutionPhase.OPEN)
    assert repeated.blocked
    assert len(broker.submit_calls) == 1


def test_live_execution_is_disabled_by_default(tmp_path):
    coordinator, _, _, broker, _ = build_coordinator(
        tmp_path, [{"fills": [(10, "100.00")]}]
    )
    coordinator._enabled = False
    with pytest.raises(PermissionError, match="disabled"):
        coordinator.execute(make_position(), ExecutionPhase.OPEN)
    assert broker.submit_calls == []


def test_close_partial_fill_is_cancelled_and_replaced_only_after_confirmation(tmp_path):
    coordinator, orders, positions, broker, _ = build_coordinator(
        tmp_path,
        [
            {"fills": [(10, "100.00")]},
            {"fills": [(10, "101.00")]},
            {"fills": [(5, "99.00")]},
            {"fills": [(5, "98.00")]},
            {"fills": [(10, "97.00")]},
        ],
    )
    position = make_position()
    opened = coordinator.execute(position, ExecutionPhase.OPEN)
    assert opened.completed

    closed = coordinator.execute(position, ExecutionPhase.CLOSE)
    assert closed.completed and not closed.blocked, (
        closed, orders.all_snapshots(), broker.cancel_calls
    )
    assert len(broker.cancel_calls) == 1
    assert [call[0].side for call in broker.submit_calls] == [
        "BUY", "SELL", "BUY", "BUY", "SELL"
    ]
    assert positions.snapshot(str(position.position_id))["is_fully_closed"]


def test_broker_complete_without_trade_ledger_fill_does_not_advance(tmp_path):
    coordinator, orders, positions, broker, _ = build_coordinator(
        tmp_path,
        [
            {"status": BrokerOrderStatus.FILLED, "fills": []},
        ],
    )
    position = make_position()
    report = coordinator.execute(position, ExecutionPhase.OPEN)
    assert report.blocked
    assert len(broker.submit_calls) == 1
    assert positions.snapshot(str(position.position_id))["status"] == "ERROR"
