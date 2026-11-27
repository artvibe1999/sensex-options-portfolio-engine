from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sensex_engine.accounting import PortfolioAccountingService
from sensex_engine.persistence import SqliteStore
from sensex_engine.reconciliation import ReconciliationReport


def make_account(tmp_path):
    store = SqliteStore(tmp_path / "accounting.sqlite3")
    return store, PortfolioAccountingService(store=store)


def test_account_ledger_persists_cash_flows_and_separates_them_from_profit(tmp_path):
    _, account = make_account(tmp_path)
    now = datetime.now(UTC)
    account.set_opening_equity(Decimal("100000"), idempotency_key="opening")
    account.record_cash_flow(
        Decimal("10000"), direction="DEPOSIT", idempotency_key="deposit",
        occurred_at=now,
    )
    account.record_cash_flow(
        Decimal("2000"), direction="WITHDRAWAL", idempotency_key="withdrawal",
        occurred_at=now,
    )
    account.record_trade_result(
        realized_pnl=Decimal("1000"), fees=Decimal("100"),
        idempotency_key="trade-1", occurred_at=now,
    )
    account.record_reconciled_mark(
        broker_net_equity=Decimal("108200"), unrealized_pnl=Decimal("-700"),
        available_margin=Decimal("50000"), open_max_loss=Decimal("2500"),
        open_positions=1, entries_today=1, reconciliation_id="recon-1",
        reconciliation_report=ReconciliationReport(True, ((1, -1),), ((1, -1),), (), ()), marked_at=now,
    )
    snapshot = account.snapshot(now=now)
    assert snapshot is not None
    assert snapshot.net_liquidation_equity == Decimal("108200")
    assert snapshot.daily_realized_pnl == Decimal("900")
    assert snapshot.daily_unrealized_pnl == Decimal("-700")
    assert snapshot.deposits_withdrawals_today == Decimal("8000")
    report = account.report(now=now)
    assert report is not None
    assert report["realized_net_pnl"] == "900"
    assert report["deposits"] == "10000"
    assert report["withdrawals"] == "2000"


def test_account_ledger_is_restart_safe_and_idempotent(tmp_path):
    store, account = make_account(tmp_path)
    account.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    assert account.set_opening_equity(Decimal("50000"), idempotency_key="opening") is False
    with pytest.raises(ValueError, match="immutable"):
        account.set_opening_equity(Decimal("60000"), idempotency_key="opening-2")
    restored = PortfolioAccountingService(store=SqliteStore(store.database_path))
    assert restored._events()[0]["payload"]["amount"] == "50000"


def test_account_ledger_rejects_conflicting_idempotency_key(tmp_path):
    _, account = make_account(tmp_path)
    account.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    account.record_cash_flow(
        Decimal("1000"), direction="DEPOSIT", idempotency_key="cash-1",
    )
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        account.record_cash_flow(
            Decimal("2000"), direction="DEPOSIT", idempotency_key="cash-1",
        )


def test_snapshot_fails_closed_without_opening_or_fresh_reconciled_mark(tmp_path):
    _, account = make_account(tmp_path)
    now = datetime.now(UTC)
    assert account.snapshot(now=now) is None
    account.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    assert account.snapshot(now=now) is None
    account.record_reconciled_mark(
        broker_net_equity=Decimal("50000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("30000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=0, reconciliation_id="old",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=now - timedelta(minutes=5),
    )
    assert account.snapshot(now=now, max_mark_age=timedelta(minutes=2)) is None


def test_snapshot_rejects_disagreement_with_broker_net_equity(tmp_path):
    _, account = make_account(tmp_path)
    now = datetime.now(UTC)
    account.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    account.record_reconciled_mark(
        broker_net_equity=Decimal("52000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("30000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=0, reconciliation_id="mismatch",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=now,
    )
    assert account.snapshot(now=now) is None


def test_account_ledger_rejects_naive_timestamps_and_invalid_amounts(tmp_path):
    _, account = make_account(tmp_path)
    with pytest.raises(ValueError):
        account.set_opening_equity(Decimal("NaN"), idempotency_key="opening")
    account.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    with pytest.raises(ValueError, match="timezone-aware"):
        account.record_cash_flow(
            Decimal("10"), direction="DEPOSIT", idempotency_key="bad-time",
            occurred_at=datetime(2026, 1, 1),
        )



def test_drawdown_is_flow_adjusted_and_does_not_treat_deposit_as_profit(tmp_path):
    _, account = make_account(tmp_path)
    day1 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    day2 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    day3 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    account.set_opening_equity(Decimal("100000"), idempotency_key="opening")
    account.record_reconciled_mark(
        broker_net_equity=Decimal("100000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("80000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=0, reconciliation_id="mark-1",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=day1,
    )
    account.record_cash_flow(
        Decimal("50000"), direction="DEPOSIT", idempotency_key="deposit",
        occurred_at=day2,
    )
    account.record_reconciled_mark(
        broker_net_equity=Decimal("150000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("120000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=0, reconciliation_id="mark-2",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=day2,
    )
    account.record_trade_result(
        realized_pnl=Decimal("-10000"), fees=Decimal("0"),
        idempotency_key="loss", occurred_at=day3,
    )
    account.record_reconciled_mark(
        broker_net_equity=Decimal("140000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("110000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=1, reconciliation_id="mark-3",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=day3,
    )
    snapshot = account.snapshot(now=day3)
    assert snapshot is not None
    assert snapshot.net_liquidation_equity == Decimal("140000")
    assert snapshot.weekly_drawdown_pct == Decimal("0.10")
    assert snapshot.peak_equity / snapshot.net_liquidation_equity == Decimal("1") / Decimal("0.9")



def test_strategy_lot_sizing_uses_live_equity_risk_allocation_and_margin(tmp_path):
    _, account = make_account(tmp_path)
    now = datetime.now(UTC)
    account.set_opening_equity(Decimal("100000"), idempotency_key="opening")
    account.record_reconciled_mark(
        broker_net_equity=Decimal("100000"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("10000"), open_max_loss=Decimal("1000"),
        open_positions=1, entries_today=1, reconciliation_id="sizing",
        reconciliation_report=ReconciliationReport(True, ((1, 1),), ((1, 1),), (), ()), marked_at=now,
    )
    lots = account.size_strategy_lots(
        strategy_risk_pct=Decimal("0.01"),
        aggregate_risk_pct=Decimal("0.02"),
        allocation_pct=Decimal("0.50"),
        max_loss_per_lot=Decimal("400"),
        estimated_cost_per_lot=Decimal("20"),
        margin_per_lot=Decimal("2000"),
        max_lots=10,
        now=now,
    )
    assert lots == 2


def test_strategy_lot_sizing_fails_closed_without_authoritative_snapshot(tmp_path):
    _, account = make_account(tmp_path)
    assert account.size_strategy_lots(
        strategy_risk_pct=Decimal("0.01"),
        aggregate_risk_pct=Decimal("0.02"),
        allocation_pct=Decimal("0.50"),
        max_loss_per_lot=Decimal("400"),
        estimated_cost_per_lot=Decimal("20"),
        margin_per_lot=Decimal("2000"),
        max_lots=10,
    ) == 0
