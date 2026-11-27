"""Size a strategy against fresh portfolio state and reapprove the scaled payoff."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .capital import adaptive_risk_budget, affordable_lots
from .domain import PortfolioSnapshot, Side, StrategyCandidate
from .market_data import validate_quote
from .payoff import calculate_expiry_payoff
from .risk import RiskEngine


class EntrySizingService:
    """Use current executable prices and broker basket margin, not fixed capital."""

    def __init__(self, *, broker: Any, snapshot_provider: Any, risk: RiskEngine) -> None:
        self.broker = broker
        self.snapshot_provider = snapshot_provider
        self.risk = risk

    def approve(
        self, candidate: StrategyCandidate, *, max_lots: int,
        allocation_pct: Decimal, now: datetime | None = None,
    ) -> tuple[StrategyCandidate, dict[int, Decimal]]:
        now = now or datetime.now(UTC)
        if not timedelta(0) <= now - candidate.data_timestamp <= timedelta(seconds=2):
            raise ValueError("Strategy signal is stale or future-dated")
        if max_lots < 1 or not Decimal("0") < allocation_pct <= Decimal("1"):
            raise ValueError("Invalid lot or allocation limit")
        if any(leg.lots != 1 for leg in candidate.legs):
            raise ValueError("Sizing input must be a one-lot candidate")
        snapshot = self.snapshot_provider()
        if not isinstance(snapshot, PortfolioSnapshot) or snapshot.peak_equity is None:
            raise ValueError("Fresh reconciled account equity is required")
        contracts = {c.instrument_token: c for c in self.broker.instruments()}
        if any(contracts.get(leg.contract.instrument_token) != leg.contract
               for leg in candidate.legs):
            raise ValueError("Candidate contract metadata is stale")
        quotes = self.broker.quotes(list(leg.contract.instrument_token for leg in candidate.legs))
        prices = {}
        quote_times = []
        for leg in candidate.legs:
            token = leg.contract.instrument_token
            quote = quotes[token]
            if not validate_quote(quote, now=datetime.now(UTC), max_age_seconds=2).valid:
                raise ValueError("Candidate quote is unavailable or stale")
            prices[token] = quote.ask if leg.side is Side.BUY else quote.bid
            quote_times.append(quote.timestamp)
        if max(quote_times) - min(quote_times) > timedelta(seconds=2):
            raise ValueError("Candidate quotes are not aligned")
        one = calculate_expiry_payoff(candidate.legs, prices)
        limits = self.risk.limits
        budget = adaptive_risk_budget(
            snapshot.net_liquidation_equity, peak_equity=snapshot.peak_equity,
            base_risk_pct=min(limits.risk_per_trade_pct, limits.max_strategy_risk_pct),
            base_aggregate_pct=limits.max_aggregate_open_risk_pct,
        )
        if not budget.new_entries_allowed:
            raise ValueError(budget.reason)
        margin_one = self.broker.basket_margin(candidate.legs, prices)
        cap = min(
            snapshot.available_margin,
            snapshot.net_liquidation_equity * allocation_pct,
            snapshot.net_liquidation_equity * limits.max_margin_utilization_pct,
        )
        lots = affordable_lots(
            risk_budget=min(budget.max_trade_risk,
                            budget.max_aggregate_risk - snapshot.open_max_loss),
            max_loss_per_lot=one.max_loss,
            estimated_cost_per_lot=candidate.estimated_costs,
            available_margin=cap, margin_per_lot=margin_one, max_lots=max_lots,
        )
        while lots:
            legs = tuple(replace(leg, lots=lots) for leg in candidate.legs)
            payoff = calculate_expiry_payoff(legs, prices)
            scaled = replace(
                candidate, legs=legs, max_loss=payoff.max_loss,
                max_profit=payoff.max_profit, net_debit=payoff.net_debit,
                estimated_costs=candidate.estimated_costs * lots,
                data_timestamp=min(quote_times),
            )
            margin = self.broker.basket_margin(legs, prices)
            fresh = datetime.now(UTC) - min(quote_times) <= timedelta(seconds=2)
            decision = self.risk.evaluate(
                scaled, snapshot, estimated_margin=margin, data_is_fresh=fresh,
                contracts_are_current=True, broker_reconciled=True,
            )
            if decision.approved and margin <= cap:
                return scaled, prices
            if not fresh:
                raise ValueError("Quotes expired during risk approval")
            lots -= 1
        raise ValueError("No whole lots pass portfolio and broker margin limits")
