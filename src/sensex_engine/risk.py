"""Portfolio-level pre-trade controls. Reject by default on incomplete inputs."""
from dataclasses import dataclass
from decimal import Decimal

from .domain import PortfolioSnapshot, RiskDecision, StrategyCandidate


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: Decimal = Decimal("0.0075")
    max_strategy_risk_pct: Decimal = Decimal("0.01")
    max_aggregate_open_risk_pct: Decimal = Decimal("0.02")
    daily_loss_stop_pct: Decimal = Decimal("0.02")
    weekly_drawdown_stop_pct: Decimal = Decimal("0.04")
    monthly_drawdown_stop_pct: Decimal = Decimal("0.07")
    max_concurrent_positions: int = 2
    max_new_entries_per_day: int = 3
    max_margin_utilization_pct: Decimal = Decimal("0.70")


class RiskEngine:
    def __init__(self, limits: RiskLimits = RiskLimits()) -> None:
        self.limits = limits

    def evaluate(
        self,
        candidate: StrategyCandidate,
        portfolio: PortfolioSnapshot,
        *,
        estimated_margin: Decimal,
        data_is_fresh: bool,
        contracts_are_current: bool,
        broker_reconciled: bool,
    ) -> RiskDecision:
        reject = lambda reason: RiskDecision(False, reason)
        equity = portfolio.net_liquidation_equity
        if equity <= 0:
            return reject("Net liquidation equity is not positive")
        if not data_is_fresh:
            return reject("Market data is stale or unavailable")
        if not contracts_are_current:
            return reject("Contract master is missing or stale")
        if not broker_reconciled:
            return reject("Broker positions/orders are not reconciled")
        if candidate.regime.value in {"transition", "unknown"}:
            return reject("Market regime is uncertain")
        if not candidate.is_defined_risk:
            return reject("Unbounded-risk strategy is prohibited")
        if candidate.max_loss <= 0 or estimated_margin <= 0:
            return reject("Invalid max loss or margin estimate")
        if portfolio.open_positions >= self.limits.max_concurrent_positions:
            return reject("Maximum concurrent positions reached")
        if portfolio.entries_today >= self.limits.max_new_entries_per_day:
            return reject("Maximum daily entries reached")
        daily_net = portfolio.daily_realized_pnl + portfolio.daily_unrealized_pnl
        if daily_net <= -(equity * self.limits.daily_loss_stop_pct):
            return reject("Daily loss stop reached")
        if portfolio.weekly_drawdown_pct >= self.limits.weekly_drawdown_stop_pct:
            return reject("Weekly drawdown stop reached")
        if portfolio.monthly_drawdown_pct >= self.limits.monthly_drawdown_stop_pct:
            return reject("Monthly drawdown stop reached")
        if estimated_margin > portfolio.available_margin:
            return reject("Insufficient available margin")
        if estimated_margin > equity * self.limits.max_margin_utilization_pct:
            return reject("Margin utilization would exceed configured ceiling")

        per_trade_cap = min(
            equity * self.limits.risk_per_trade_pct,
            equity * self.limits.max_strategy_risk_pct,
        )
        if candidate.max_loss + candidate.estimated_costs > per_trade_cap:
            return RiskDecision(False, "Candidate exceeds per-trade risk budget", per_trade_cap)
        aggregate_cap = equity * self.limits.max_aggregate_open_risk_pct
        remaining = aggregate_cap - portfolio.open_max_loss
        if candidate.max_loss + candidate.estimated_costs > remaining:
            return RiskDecision(False, "Candidate exceeds aggregate open-risk budget", max(remaining, Decimal("0")))
        return RiskDecision(True, "Approved by pre-trade risk checks", per_trade_cap)
