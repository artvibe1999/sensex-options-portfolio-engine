from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sensex_engine.capital import EquityLedger, adaptive_risk_budget, affordable_lots
from sensex_engine.market_data import Quote, validate_quote


def test_deposits_are_not_counted_as_trading_profit():
    ledger = EquityLedger(
        opening_equity=Decimal("50000"),
        deposits=Decimal("10000"),
        realized_pnl=Decimal("1000"),
        fees_and_taxes=Decimal("100"),
    )
    assert ledger.net_liquidation_equity == Decimal("60900")


def test_risk_budget_reduces_with_drawdown_and_pauses():
    reduced = adaptive_risk_budget(Decimal("48000"), peak_equity=Decimal("50000"))
    assert reduced.risk_pct == Decimal("0.005625")
    paused = adaptive_risk_budget(Decimal("46500"), peak_equity=Decimal("50000"))
    assert not paused.new_entries_allowed
    assert paused.max_trade_risk == 0


def test_lot_sizing_respects_both_risk_and_margin():
    lots = affordable_lots(
        risk_budget=Decimal("1000"),
        max_loss_per_lot=Decimal("300"),
        estimated_cost_per_lot=Decimal("20"),
        available_margin=Decimal("2500"),
        margin_per_lot=Decimal("1000"),
        max_lots=10,
    )
    assert lots == 2


def test_quote_quality_rejects_stale_and_wide_quotes():
    now = datetime.now(UTC)
    stale = Quote(1, now - timedelta(seconds=10), Decimal("100"), Decimal("99"),
                  Decimal("101"), 10, 10, 100)
    assert not validate_quote(stale, now=now).valid
    wide = Quote(1, now, Decimal("100"), Decimal("90"), Decimal("110"), 10, 10, 100)
    assert not validate_quote(wide, now=now).valid


def test_quote_quality_accepts_fresh_liquid_quote():
    now = datetime.now(UTC)
    quote = Quote(1, now, Decimal("100"), Decimal("99.5"), Decimal("100.5"), 20, 25, 1000)
    assert validate_quote(quote, now=now).valid


def test_quote_quality_rejects_zero_traded_volume():
    now = datetime.now(UTC)
    quote = Quote(
        1, now, Decimal("100"), Decimal("99.5"), Decimal("100.5"),
        20, 25, 0,
    )
    result = validate_quote(quote, now=now)
    assert not result.valid
    assert result.reason == "Insufficient traded volume"
