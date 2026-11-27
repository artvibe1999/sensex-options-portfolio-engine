from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sensex_engine.domain import Contract, Right, TradingMode
from sensex_engine.emergency_close import (
    EmergencyCloseAllService,
    build_emergency_limit_price_provider,
)
from sensex_engine.mode import ModeController
from sensex_engine.market_data import Quote
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.telegram_auth import TelegramAllowlist
from sensex_engine.telegram_control import OperationsController


def _contract():
    return Contract(
        "SENSEX26DEC80000CE", 1001,
        datetime(2026, 12, 31, tzinfo=UTC),
        Decimal("80000"), Right.CE, 20, Decimal("0.05"),
    )


class FakeBroker:
    def __init__(self, *, net=None, orders=None):
        self.net = net or []
        self.order_rows = orders or []
        self.authenticated = True
        self.position_calls = 0

    def is_authenticated(self):
        return self.authenticated

    def instruments(self):
        return [_contract()]

    def positions(self):
        self.position_calls += 1
        return {"net": list(self.net)}

    def orders(self):
        return list(self.order_rows)

    def quotes(self, tokens):
        now = datetime.now(UTC)
        return {
            token: Quote(
                instrument_token=token, timestamp=now,
                last_price=Decimal("100"), bid=Decimal("99.95"),
                ask=Decimal("100.05"), bid_quantity=10, ask_quantity=10, volume=100,
            )
            for token in tokens
        }


class FakeTradeHistory:
    def __init__(self, pending=False):
        self.pending = pending
        self.polls = []

    def poll(self, order_ids):
        self.polls.append(tuple(order_ids))

    def poll_current_session(self, *, session_date):
        self.current_session = session_date

    def recover_pending(self):
        return 0

    def has_pending_receipts(self):
        return self.pending


class FakeSubmission:
    def __init__(self, orders, *, confirm_cancel=True):
        self.orders = orders
        self.confirm_cancel = confirm_cancel
        self.cancel_calls = []

    def refresh_order(self, order_id):
        snapshot = self.orders.snapshot(order_id)
        if (
            self.confirm_cancel
            and snapshot is not None
            and snapshot["state"] == OrderState.CANCEL_PENDING.value
        ):
            self.orders.transition(
                order_id=order_id,
                new_state=OrderState.CANCELLED,
                update_id="fake-broker-cancelled",
                broker_order_id=snapshot["broker_order_id"],
            )

    def cancel_once(self, order_id):
        self.cancel_calls.append(order_id)
        self.orders.transition(
            order_id=order_id,
            new_state=OrderState.CANCEL_PENDING,
            update_id="fake-cancel-request",
        )


class FakeCoordinator:
    def __init__(self):
        self.calls = []

    def execute(self, position, phase):
        self.calls.append((position, phase))
        raise AssertionError("No positions should require closing in this test")


def _service(tmp_path, *, broker=None, static_ip=True, submission=None, history=None, coordinator=None):
    store = SqliteStore(tmp_path / "emergency.sqlite3")
    orders = OrderLifecycle(store)
    positions = PositionLifecycle(store)
    operations = OperationsController(
        TelegramAllowlist(frozenset({11}), frozenset({22})),
        ModeController(mode=TradingMode.LIVE),
        paused=False,
    )
    broker = broker or FakeBroker()
    history = history or FakeTradeHistory()
    submission = submission or FakeSubmission(orders)
    coordinator = coordinator or FakeCoordinator()
    service = EmergencyCloseAllService(
        operations=operations,
        broker=broker,
        contracts_provider=broker.instruments,
        orders=orders,
        positions=positions,
        submission=submission,
        trade_history=history,
        coordinator=coordinator,
        store=store,
        static_ip_confirmed=static_ip,
    )
    return service, operations, store, orders, positions, submission, history, coordinator


def test_emergency_close_all_halts_and_verifies_flat_broker(tmp_path):
    service, operations, store, *_ = _service(tmp_path)
    report = service.execute()
    assert report.completed
    assert report.halted
    assert operations.paused
    assert operations.mode.mode is TradingMode.PAPER
    assert store.events_for(report.run_id)[0]["event_type"] == "EMERGENCY_CLOSE_ALL_STARTED"
    assert store.events_for(report.run_id)[-1]["event_type"] == "EMERGENCY_CLOSE_ALL_FINISHED"


def test_emergency_close_all_refuses_unmanaged_broker_exposure(tmp_path):
    broker = FakeBroker(net=[{
        "exchange": "BFO", "tradingsymbol": "SENSEX26DEC80000CE",
        "instrument_token": 1001, "quantity": 20,
    }])
    service, operations, _, *_ = _service(tmp_path, broker=broker)
    report = service.execute()
    assert not report.completed
    assert report.halted and operations.paused
    assert report.residual_exposure == (("SENSEX26DEC80000CE", 20),)
    assert any("mismatch" in item.lower() for item in report.reasons)


def test_emergency_close_all_requires_static_ip_confirmation(tmp_path):
    broker = FakeBroker()
    service, operations, _, *_ = _service(tmp_path, broker=broker, static_ip=False)
    report = service.execute()
    assert not report.completed
    assert operations.paused and operations.mode.mode is TradingMode.PAPER
    assert broker.position_calls == 0
    assert any("static" in item.lower() for item in report.reasons)


def test_emergency_close_all_cancels_confirmed_pending_entry_before_close(tmp_path):
    service, _, _, orders, _, submission, _, coordinator = _service(tmp_path)
    order_id = "position-1:OPEN:0:1"
    orders.create_intent(
        order_id=order_id, position_id="position-1", leg_index=0,
        instrument_token=1001, tradingsymbol="SENSEX26DEC80000CE",
        side="BUY", quantity=20, order_type="LIMIT", product="NRML",
        mode="live", idempotency_key="intent:pending-entry",
        limit_price=Decimal("100"),
    )
    for state, update_id in [
        (OrderState.VALIDATED, "validated"),
        (OrderState.SUBMITTING, "submitting"),
        (OrderState.ACKNOWLEDGED, "acknowledged"),
        (OrderState.OPEN, "open"),
    ]:
        orders.transition(
            order_id=order_id, new_state=state, update_id=update_id,
            broker_order_id="broker-1" if state is OrderState.ACKNOWLEDGED else None,
        )
    report = service.execute()
    assert report.completed
    assert submission.cancel_calls == [order_id]
    assert order_id in report.cancelled_entry_orders
    assert not coordinator.calls


def test_emergency_close_all_blocks_until_cancel_is_broker_confirmed(tmp_path):
    service, _, _, orders, _, submission, _, coordinator = _service(
        tmp_path, submission=None
    )
    submission.confirm_cancel = False
    order_id = "position-2:OPEN:0:1"
    orders.create_intent(
        order_id=order_id, position_id="position-2", leg_index=0,
        instrument_token=1001, tradingsymbol="SENSEX26DEC80000CE",
        side="BUY", quantity=20, order_type="LIMIT", product="NRML",
        mode="live", idempotency_key="intent:uncertain-entry",
        limit_price=Decimal("100"),
    )
    for state, update_id in [
        (OrderState.VALIDATED, "validated"),
        (OrderState.SUBMITTING, "submitting"),
        (OrderState.ACKNOWLEDGED, "acknowledged"),
        (OrderState.OPEN, "open"),
    ]:
        orders.transition(
            order_id=order_id, new_state=state, update_id=update_id,
            broker_order_id="broker-2" if state is OrderState.ACKNOWLEDGED else None,
        )
    report = service.execute()
    assert not report.completed
    assert order_id in report.unresolved_orders
    assert not coordinator.calls


def test_emergency_close_all_rejects_unprojected_fill_receipts(tmp_path):
    history = FakeTradeHistory(pending=True)
    service, _, _, *_ = _service(tmp_path, history=history)
    report = service.execute()
    assert not report.completed
    assert "BROKER_FILL_PROJECTION" in report.unresolved_orders


def test_emergency_limit_prices_use_fresh_executable_side_and_tick_rounding():
    broker = FakeBroker()
    price = build_emergency_limit_price_provider(broker)
    buy = SimpleNamespace(instrument_token=1001, tradingsymbol="SENSEX26DEC80000CE", side="BUY")
    sell = SimpleNamespace(instrument_token=1001, tradingsymbol="SENSEX26DEC80000CE", side="SELL")
    assert price(buy) == Decimal("100.05")
    assert price(sell) == Decimal("99.95")
