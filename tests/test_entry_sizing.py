from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sensex_engine.domain import (
    Contract,
    OptionLeg,
    PortfolioSnapshot,
    Regime,
    Right,
    Side,
    StrategyCandidate,
)
from sensex_engine.entry_sizing import EntrySizingService
from sensex_engine.market_data import Quote
from sensex_engine.risk import RiskEngine


def test_sizing_recalculates_payoff_and_margin_at_actual_quantity():
    now = datetime.now(UTC)
    expiry = now + timedelta(days=5)
    long = Contract("A", 1, expiry, Decimal("80000"), Right.CE, 20, Decimal(".05"))
    short = Contract("B", 2, expiry, Decimal("80100"), Right.CE, 20, Decimal(".05"))
    legs = (OptionLeg(long, Side.BUY, 1), OptionLeg(short, Side.SELL, 1))
    candidate = StrategyCandidate(
        "VERTICAL", Regime.BULL_TREND, legs, Decimal("10"), Decimal("100"),
        Decimal("10"), Decimal("10"), now, True,
    )

    class Broker:
        def instruments(self):
            return [long, short]

        def quotes(self, tokens):
            return {
                1: Quote(1, now, Decimal("100"), Decimal("99"), Decimal("100"), 100, 100, 500),
                2: Quote(2, now, Decimal("90"), Decimal("90"), Decimal("91"), 100, 100, 500),
            }

        def basket_margin(self, legs, prices):
            return Decimal("2000") * legs[0].lots

    snapshot = PortfolioSnapshot(
        Decimal("100000"), Decimal("10000"), Decimal("0"), Decimal("0"), Decimal("0"),
        peak_equity=Decimal("100000"),
    )
    sizing = EntrySizingService(
        broker=Broker(), snapshot_provider=lambda: snapshot, risk=RiskEngine(),
    )
    approved, prices = sizing.approve(candidate, max_lots=10, allocation_pct=Decimal(".5"))
    assert approved.legs[0].lots == 3
    assert approved.max_loss == Decimal("600")
    assert approved.estimated_costs == Decimal("30")
    assert prices == {1: Decimal("100"), 2: Decimal("90")}

    sizing = EntrySizingService(
        broker=Broker(), snapshot_provider=lambda: snapshot, risk=RiskEngine(),
        quantity_freeze_units=40,
    )
    capped, _ = sizing.approve(candidate, max_lots=10, allocation_pct=Decimal(".5"))
    assert capped.legs[0].lots == 2

    sizing = EntrySizingService(
        broker=Broker(), snapshot_provider=lambda: None, risk=RiskEngine(),
    )
    with pytest.raises(ValueError, match="reconciled"):
        sizing.approve(candidate, max_lots=10, allocation_pct=Decimal(".5"))
