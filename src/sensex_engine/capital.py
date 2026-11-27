"""Dynamic equity accounting and risk-budget sizing helpers."""
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal


@dataclass(frozen=True)
class EquityLedger:
    """Cash-flow-aware equity estimate; deposits are not trading profit."""

    opening_equity: Decimal
    deposits: Decimal = Decimal("0")
    withdrawals: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_and_taxes: Decimal = Decimal("0")

    @property
    def net_liquidation_equity(self) -> Decimal:
        return (
            self.opening_equity
            + self.deposits
            - self.withdrawals
            + self.realized_pnl
            + self.unrealized_pnl
            - self.fees_and_taxes
        )


@dataclass(frozen=True)
class AdaptiveRiskBudget:
    risk_pct: Decimal
    max_trade_risk: Decimal
    max_aggregate_risk: Decimal
    new_entries_allowed: bool
    reason: str


def adaptive_risk_budget(
    equity: Decimal,
    *,
    peak_equity: Decimal,
    base_risk_pct: Decimal = Decimal("0.0075"),
    base_aggregate_pct: Decimal = Decimal("0.02"),
    band_scale: Decimal = Decimal("1"),
) -> AdaptiveRiskBudget:
    """Reduce risk as peak-to-current drawdown rises; never scale beyond base.

    ``band_scale`` widens the 3/5/7/10% drawdown bands proportionally for
    capital tiers that take more than the baseline risk per trade.
    """
    if not band_scale.is_finite() or band_scale < 1:
        raise ValueError("Drawdown band scale must be at least 1")
    if equity <= 0 or peak_equity <= 0:
        return AdaptiveRiskBudget(
            Decimal("0"), Decimal("0"), Decimal("0"), False, "Invalid equity"
        )
    drawdown = max(Decimal("0"), (peak_equity - equity) / peak_equity)
    if drawdown >= Decimal("0.10") * band_scale:
        return AdaptiveRiskBudget(
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            False,
            "Trading suspended at 10% drawdown",
        )
    multiplier = Decimal("1")
    reason = "Normal risk budget"
    if drawdown >= Decimal("0.07") * band_scale:
        multiplier, reason = Decimal("0"), "Trading paused for drawdown review"
    elif drawdown >= Decimal("0.05") * band_scale:
        multiplier, reason = Decimal("0.50"), "Risk reduced 50% at 5-7% drawdown"
    elif drawdown >= Decimal("0.03") * band_scale:
        multiplier, reason = Decimal("0.75"), "Risk reduced 25% at 3-5% drawdown"
    return AdaptiveRiskBudget(
        risk_pct=base_risk_pct * multiplier,
        max_trade_risk=equity * base_risk_pct * multiplier,
        max_aggregate_risk=equity * base_aggregate_pct * multiplier,
        new_entries_allowed=multiplier > 0,
        reason=reason,
    )


def affordable_lots(
    *,
    risk_budget: Decimal,
    max_loss_per_lot: Decimal,
    estimated_cost_per_lot: Decimal,
    available_margin: Decimal,
    margin_per_lot: Decimal,
    max_lots: int,
) -> int:
    """Return the lesser of risk- and margin-affordable lots; invalid inputs fail closed."""
    if (
        risk_budget <= 0
        or max_loss_per_lot <= 0
        or estimated_cost_per_lot < 0
        or available_margin <= 0
        or margin_per_lot <= 0
        or max_lots <= 0
    ):
        return 0
    risk_per_lot = max_loss_per_lot + estimated_cost_per_lot
    by_risk = int((risk_budget / risk_per_lot).to_integral_value(rounding=ROUND_FLOOR))
    by_margin = int((available_margin / margin_per_lot).to_integral_value(rounding=ROUND_FLOOR))
    return max(0, min(by_risk, by_margin, max_lots))
