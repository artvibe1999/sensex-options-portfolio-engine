from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.startup_recovery import KiteStartupRecoveryService


class EmptyBroker:
    def is_authenticated(self):
        return True

    def positions(self):
        return {"net": []}

    def orders(self):
        return []


class FakeFillIngestion:
    def __init__(self, result=None, error=None):
        self.result = list(result or [])
        self.error = error
        self.calls = 0

    def recover_pending(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeTradeHistory:
    def __init__(self, result=None):
        self.result = list(result or [])
        self.sessions = []

    def poll_current_session(self, *, session_date):
        self.sessions.append(session_date)
        return self.result


def make_service(tmp_path, *, contracts=None, receipt_result=None, fill_result=None,
                 receipt_error=None):
    store = SqliteStore(tmp_path / "startup-recovery.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    ingestion = FakeFillIngestion(receipt_result, receipt_error)
    history = FakeTradeHistory(fill_result)
    contract = SimpleNamespace(
        instrument_token=1001, tradingsymbol="SENSEX_CE1", lot_size=10
    )
    service = KiteStartupRecoveryService(
        broker=EmptyBroker(),
        contracts_provider=lambda: (
            [contract] if contracts is None else contracts
        ),
        position_lifecycle=positions,
        order_lifecycle=orders,
        fill_ingestion=ingestion,
        trade_history=history,
    )
    return service, ingestion, history


def current_ist_date():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def test_startup_recovery_replays_then_backfills_then_reconciles(tmp_path):
    service, ingestion, history = make_service(
        tmp_path, receipt_result=["receipt-1"], fill_result=["fill-1"]
    )

    result = service.recover(session_date=current_ist_date())

    assert result.ready
    assert result.recovered_receipts == 1
    assert result.backfilled_fills == 1
    assert result.reconciliation is not None
    assert result.reconciliation.reconciled
    assert ingestion.calls == 1
    assert history.sessions == [current_ist_date()]


def test_startup_recovery_stops_before_broker_poll_if_receipt_replay_fails(tmp_path):
    service, ingestion, history = make_service(
        tmp_path, receipt_error=RuntimeError("receipt store unavailable")
    )

    result = service.recover(session_date=current_ist_date())

    assert not result.ready
    assert result.reconciliation is None
    assert result.reasons == ("Startup recovery failed: RuntimeError",)
    assert ingestion.calls == 1
    assert history.sessions == []


def test_startup_recovery_rejects_empty_contract_master(tmp_path):
    service, _, history = make_service(tmp_path, contracts=[])

    result = service.recover(session_date=current_ist_date())

    assert not result.ready
    assert result.reconciliation is None
    assert "contract master" in result.reasons[0]
    assert history.sessions == [current_ist_date()]


def test_startup_recovery_rejects_prior_session_before_side_effects(tmp_path):
    service, ingestion, history = make_service(tmp_path)

    with pytest.raises(ValueError, match="current IST session"):
        service.recover(session_date=date(2000, 1, 1))

    assert ingestion.calls == 0
    assert history.sessions == []


def test_startup_recovery_requires_explicit_calendar_date(tmp_path):
    service, ingestion, history = make_service(tmp_path)

    with pytest.raises(ValueError, match="explicit calendar date"):
        service.recover(session_date="today")

    assert ingestion.calls == 0
    assert history.sessions == []
