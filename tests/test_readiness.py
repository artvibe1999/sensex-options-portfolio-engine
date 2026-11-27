from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.readiness import LiveReadinessService


class FakeBroker:
    def is_authenticated(self):
        return True

    def positions(self):
        return {"net": []}

    def orders(self):
        return []


def make_service(
    tmp_path, *, contract_age=timedelta(seconds=0),
    market_age=timedelta(seconds=0), risk=True,
):
    now = datetime.now(UTC)
    contract = SimpleNamespace(instrument_token=1, tradingsymbol="SENSEX80000CE", lot_size=1)
    store = SqliteStore(tmp_path / "readiness.sqlite3")
    lifecycle = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    return LiveReadinessService(
        broker=FakeBroker(),
        position_lifecycle=lifecycle,
        order_lifecycle=orders,
        contracts_provider=lambda: ([contract], now - contract_age),
        market_data_timestamp_provider=lambda: now - market_age,
        risk_clearance_provider=lambda: risk,
    ), now


def test_readiness_passes_only_when_every_gate_is_clear(tmp_path):
    service, now = make_service(tmp_path)
    result = service.evaluate(now=now)
    assert result.ready
    assert result.telegram_flags() == (True, True)


def test_readiness_rejects_stale_contract_master(tmp_path):
    service, now = make_service(tmp_path, contract_age=timedelta(days=2))
    result = service.evaluate(now=now)
    assert not result.broker_ready
    assert any("contract master is stale" in reason for reason in result.reasons)


def test_readiness_rejects_stale_market_data(tmp_path):
    service, now = make_service(tmp_path, market_age=timedelta(seconds=10))
    result = service.evaluate(now=now)
    assert not result.broker_ready
    assert any("Market data is stale" in reason for reason in result.reasons)


def test_readiness_rejects_nonaffirmative_risk_check(tmp_path):
    service, now = make_service(tmp_path, risk=False)
    result = service.evaluate(now=now)
    assert not result.ready
    assert not result.risk_clear
    assert result.telegram_flags() == (False, False)


def test_readiness_rejects_naive_timestamps(tmp_path):
    service, now = make_service(tmp_path)
    service.market_data_timestamp_provider = lambda: datetime.now()
    result = service.evaluate(now=now)
    assert not result.broker_ready
    assert any("Market data is stale" in reason for reason in result.reasons)


def test_readiness_rejects_naive_evaluation_time(tmp_path):
    service, _ = make_service(tmp_path)
    try:
        service.evaluate(now=datetime.now())
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive evaluation time must be rejected")


def test_readiness_includes_unresolved_live_order_ledger(tmp_path):
    now = datetime.now(UTC)
    store = SqliteStore(tmp_path / "readiness-live-order.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    orders.create_intent(
        order_id="uncertain-live-order", position_id="position-1",
        leg_index=0, instrument_token=1, tradingsymbol="SENSEX80000CE",
        side="BUY", quantity=20, order_type="LIMIT", product="MIS",
        mode="live", idempotency_key="uncertain-live-order-create",
    )
    contract = SimpleNamespace(
        instrument_token=1, tradingsymbol="SENSEX80000CE", lot_size=1
    )
    service = LiveReadinessService(
        broker=FakeBroker(),
        position_lifecycle=positions,
        order_lifecycle=orders,
        contracts_provider=lambda: ([contract], now),
        market_data_timestamp_provider=lambda: now,
        risk_clearance_provider=lambda: True,
    )
    result = service.evaluate(now=now)
    assert not result.ready
    assert not result.broker_ready
    assert any(
        "uncertain-live-order requires reconciliation" in reason
        for reason in result.reasons
    )
