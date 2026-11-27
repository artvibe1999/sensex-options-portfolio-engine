from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sensex_engine.domain import Contract, PortfolioSnapshot, Right
from sensex_engine.market_data import Quote
from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.runtime_readiness import (
    KitePortfolioRiskReadiness,
    build_kite_live_readiness_service,
)


class RuntimeBroker:
    def __init__(self, *, quote_age=timedelta(0), margin_net=100000):
        self.quote_age = quote_age
        self.margin_net = margin_net
        self.contracts = [
            Contract("SENSEX26DEC80000CE", 1001, datetime(2026, 12, 31, tzinfo=UTC),
                     Decimal("80000"), Right.CE, 20, Decimal("0.05")),
            Contract("SENSEX26DEC80000PE", 1002, datetime(2026, 12, 31, tzinfo=UTC),
                     Decimal("80000"), Right.PE, 20, Decimal("0.05")),
        ]

    def is_authenticated(self):
        return True

    def instruments(self):
        return list(self.contracts)

    def quotes(self, tokens):
        now = datetime.now(UTC) - self.quote_age
        return {
            token: Quote(
                instrument_token=token, timestamp=now,
                last_price=Decimal("100"), bid=Decimal("99"),
                ask=Decimal("101"), bid_quantity=20, ask_quantity=20, volume=100,
            )
            for token in tokens
        }

    def positions(self):
        return {"net": []}

    def orders(self):
        return []

    def margins(self, segment):
        assert segment == "equity"
        return {
            "enabled": True,
            "net": self.margin_net,
            "available": {"cash": self.margin_net, "live_balance": self.margin_net},
            "utilised": {"debits": 0},
        }


def _settings(static_ip=True):
    return SimpleNamespace(
        kite_static_ip_confirmed=static_ip,
        max_margin_utilization_pct=0.70,
        risk_per_trade_pct=0.0075,
        max_aggregate_open_risk_pct=0.02,
        daily_loss_stop_pct=0.02,
        weekly_drawdown_stop_pct=0.04,
        monthly_drawdown_stop_pct=0.07,
        max_concurrent_positions=2,
        max_new_entries_per_day=3,
    )


def _portfolio():
    return PortfolioSnapshot(
        net_liquidation_equity=Decimal("100000"),
        available_margin=Decimal("100000"),
        open_max_loss=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
        weekly_drawdown_pct=Decimal("0"),
        monthly_drawdown_pct=Decimal("0"),
        open_positions=0,
        entries_today=0,
        peak_equity=Decimal("100000"),
    )


def _service(tmp_path, broker, *, static_ip=True, snapshot_provider=_portfolio):
    store = SqliteStore(tmp_path / "runtime-readiness.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    service = build_kite_live_readiness_service(
        broker=broker, store=store, position_lifecycle=positions,
        order_lifecycle=orders, settings=_settings(static_ip),
        portfolio_snapshot_provider=snapshot_provider,
    )
    return service, store


def test_runtime_readiness_passes_all_real_kite_and_risk_gates(tmp_path):
    service, _ = _service(tmp_path, RuntimeBroker())
    result = service.evaluate()
    assert result.ready
    assert result.telegram_flags() == (True, True)


def test_runtime_readiness_rejects_stale_option_quotes(tmp_path):
    service, _ = _service(
        tmp_path, RuntimeBroker(quote_age=timedelta(seconds=5))
    )
    result = service.evaluate()
    assert not result.ready
    assert any("quote rejected" in reason.lower() for reason in result.reasons)


def test_runtime_readiness_rejects_missing_authoritative_portfolio_snapshot(tmp_path):
    service, _ = _service(tmp_path, RuntimeBroker(), snapshot_provider=None)
    result = service.evaluate()
    assert not result.ready
    assert not result.risk_clear
    assert any("PE-06" in reason for reason in result.reasons)


def test_runtime_readiness_requires_explicit_static_ip_confirmation(tmp_path):
    service, _ = _service(tmp_path, RuntimeBroker(), static_ip=False)
    result = service.evaluate()
    assert not result.ready
    assert any("execution prerequisites" in reason.lower() for reason in result.reasons)


def test_runtime_readiness_rejects_disabled_or_unfunded_kite_segment(tmp_path):
    broker = RuntimeBroker(margin_net=0)
    service, _ = _service(tmp_path, broker)
    result = service.evaluate()
    assert not result.ready
    assert not result.risk_clear
    assert any("funds" in reason.lower() for reason in result.reasons)


def test_portfolio_risk_gate_rejects_missing_snapshot_even_with_broker_funds():
    gate = KitePortfolioRiskReadiness(
        broker=RuntimeBroker(), snapshot_provider=None, settings=_settings()
    )
    assert gate() is False
    assert "PE-06" in gate.last_reason


def test_runtime_readiness_requires_paired_ce_and_pe_quotes(tmp_path):
    broker = RuntimeBroker()
    broker.contracts = broker.contracts[:1]
    service, _ = _service(tmp_path, broker)
    result = service.evaluate()
    assert not result.ready
    assert any("paired" in reason.lower() for reason in result.reasons)
