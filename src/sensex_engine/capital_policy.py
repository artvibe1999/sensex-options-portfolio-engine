"""Capital-tier risk policy: risk limits adapt to current verified equity.

The specification's 1%-per-trade guardrail cannot fund a single SENSEX spread
lot below roughly two lakh rupees (the narrowest liquid defined-risk spread
loses ~Rs 800-2,000 per lot). The owner asked for dynamic capital: the engine
reads current equity and picks the tier below, then strategy builders select
strike widths whose worst-case loss fits the resulting budget. Small accounts
therefore take proportionally more risk per trade, and the percentage falls
back to the specification's 1% as equity grows. Drawdown scaling (spec 9.3)
is applied on top, with bands widened in proportion to the tier so a single
normal loss does not suspend a small account.

All tiers are configuration, versioned by ``POLICY_VERSION``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .capital import adaptive_risk_budget
from .risk import RiskLimits

POLICY_VERSION = "capital-tiers-v1"
PAISE = Decimal("0.01")


@dataclass(frozen=True)
class CapitalTier:
    name: str
    min_equity: Decimal
    risk_per_trade_pct: Decimal
    max_aggregate_open_risk_pct: Decimal
    daily_loss_stop_pct: Decimal
    drawdown_band_scale: Decimal
    max_concurrent_positions: int


TIERS: tuple[CapitalTier, ...] = (
    CapitalTier(
        "micro", Decimal("0"), Decimal("0.04"), Decimal("0.08"), Decimal("0.06"), Decimal("2.0"), 2
    ),
    CapitalTier(
        "small",
        Decimal("100000"),
        Decimal("0.025"),
        Decimal("0.06"),
        Decimal("0.05"),
        Decimal("1.5"),
        3,
    ),
    CapitalTier(
        "medium",
        Decimal("300000"),
        Decimal("0.015"),
        Decimal("0.04"),
        Decimal("0.03"),
        Decimal("1.25"),
        3,
    ),
    CapitalTier(
        "standard",
        Decimal("1000000"),
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.02"),
        Decimal("1.0"),
        3,
    ),
)


@dataclass(frozen=True)
class RiskPolicy:
    tier: CapitalTier
    equity: Decimal
    drawdown_pct: Decimal
    multiplier: Decimal
    reason: str
    limits: RiskLimits
    trade_budget: Decimal  # boundary-continuous rupee budget before drawdown scaling
    aggregate_budget: Decimal

    @property
    def trade_pct(self) -> Decimal:
        return self.trade_budget / self.equity

    @property
    def max_trade_risk(self) -> Decimal:
        return (self.trade_budget * self.multiplier).quantize(PAISE, ROUND_FLOOR)

    @property
    def max_aggregate_risk(self) -> Decimal:
        return (self.aggregate_budget * self.multiplier).quantize(PAISE, ROUND_FLOOR)

    @property
    def daily_loss_limit(self) -> Decimal:
        return self.equity * self.tier.daily_loss_stop_pct

    @property
    def entries_allowed(self) -> bool:
        return self.multiplier > 0

    def describe(self) -> str:
        return (
            f"tier={self.tier.name} ({POLICY_VERSION}); equity=Rs{self.equity:,.0f}; "
            f"risk/trade={self.trade_pct * 100:.2f}% x{self.multiplier:.2f} "
            f"= Rs{self.max_trade_risk:,.0f}; aggregate cap Rs{self.max_aggregate_risk:,.0f}; "
            f"daily stop {self.limits.daily_loss_stop_pct * 100:.1f}%; drawdown "
            f"{self.drawdown_pct * 100:.2f}% ({self.reason})"
        )


def tier_budget(equity: Decimal, attr: str = "risk_per_trade_pct") -> Decimal:
    """Per-trade risk fraction that keeps the rupee budget non-decreasing in equity.

    Crossing into a lower-percentage tier never shrinks the rupee budget: the
    budget stays at the previous tier's ceiling until the new percentage
    overtakes it. R(E) = max over reached tiers of pct_i * min(E, next_tier_min).
    """
    budget = Decimal("0")
    for index, tier in enumerate(TIERS):
        if equity < tier.min_equity:
            break
        upper = TIERS[index + 1].min_equity if index + 1 < len(TIERS) else None
        capped = equity if upper is None else min(equity, upper)
        budget = max(budget, getattr(tier, attr) * capped)
    return budget


def tier_for(equity: Decimal) -> CapitalTier:
    chosen = TIERS[0]
    for tier in TIERS:
        if equity >= tier.min_equity:
            chosen = tier
    return chosen


def _tighter(tier_value: Decimal, override: Decimal | None) -> Decimal:
    return tier_value if override is None else min(tier_value, override)


def risk_policy(
    *,
    equity: Decimal,
    peak_equity: Decimal,
    max_new_entries_per_day: int = 3,
    max_margin_utilization_pct: Decimal = Decimal("0.70"),
    risk_scale: Decimal = Decimal("1"),
    daily_stop_override: Decimal | None = None,
    weekly_stop_override: Decimal | None = None,
    monthly_stop_override: Decimal | None = None,
) -> RiskPolicy:
    """Return tier limits after spec 9.3 drawdown scaling (bands scaled by tier).

    ``risk_scale`` lets the owner uniformly reduce (never above 1) every tier.
    Explicit operator stop percentages can only tighten the tier's stops.
    """
    if equity <= 0 or peak_equity <= 0:
        raise ValueError("Positive equity and peak equity are required")
    if not Decimal("0") < risk_scale <= Decimal("1"):
        raise ValueError("risk_scale must be in (0, 1]")
    tier = tier_for(equity)
    drawdown = max(Decimal("0"), (peak_equity - equity) / peak_equity)
    trade_budget = tier_budget(equity) * risk_scale
    aggregate_budget = tier_budget(equity, "max_aggregate_open_risk_pct") * risk_scale
    trade_pct = trade_budget / equity
    aggregate_pct = aggregate_budget / equity
    base = trade_pct
    budget = adaptive_risk_budget(
        equity,
        peak_equity=peak_equity,
        base_risk_pct=base,
        base_aggregate_pct=aggregate_pct,
        band_scale=tier.drawdown_band_scale,
    )
    multiplier = (budget.risk_pct / base) if base else Decimal("0")
    limits = RiskLimits(
        risk_per_trade_pct=base,
        max_strategy_risk_pct=base,
        max_aggregate_open_risk_pct=aggregate_pct,
        daily_loss_stop_pct=_tighter(tier.daily_loss_stop_pct, daily_stop_override),
        weekly_drawdown_stop_pct=_tighter(tier.daily_loss_stop_pct * 2, weekly_stop_override),
        monthly_drawdown_stop_pct=_tighter(
            tier.daily_loss_stop_pct * Decimal("3.5"), monthly_stop_override
        ),
        max_concurrent_positions=tier.max_concurrent_positions,
        max_new_entries_per_day=max_new_entries_per_day,
        max_margin_utilization_pct=max_margin_utilization_pct,
        drawdown_band_scale=tier.drawdown_band_scale,
    )
    return RiskPolicy(
        tier,
        equity,
        drawdown,
        multiplier,
        budget.reason,
        limits,
        trade_budget,
        aggregate_budget,
    )
