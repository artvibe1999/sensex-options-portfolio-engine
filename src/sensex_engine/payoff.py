"""Expiry payoff validation for same-expiry defined-risk option structures."""
from dataclasses import dataclass
from decimal import Decimal

from .domain import OptionLeg, Side
from .market_data import Quote


@dataclass(frozen=True)
class PayoffSummary:
    net_debit: Decimal
    max_loss: Decimal
    max_profit: Decimal | None
    breakevens: tuple[Decimal, ...]
    scenario_pnl: tuple[tuple[Decimal, Decimal], ...]


def calculate_expiry_payoff(
    legs: tuple[OptionLeg, ...],
    premiums: dict[int, Decimal],
    *,
    estimated_costs: Decimal = Decimal("0"),
) -> PayoffSummary:
    """Calculate exact piecewise-linear expiry payoff for a same-expiry structure.

    Premiums are per underlying unit. Contract lot sizes and requested lots are
    applied to cashflows and intrinsic value. Fees/slippage are reported as costs
    and deducted from scenario P&L, while max_loss excludes costs (the candidate
    carries costs separately for risk budgeting).
    """
    if not legs or estimated_costs < 0:
        raise ValueError("At least one leg and non-negative costs are required")
    expiries = {leg.contract.expiry for leg in legs}
    if len(expiries) != 1:
        raise ValueError("Expiry payoff validation requires a single common expiry")
    if len({leg.contract.instrument_token for leg in legs}) != len(legs):
        raise ValueError("Duplicate option contract legs are not supported")

    net_debit = Decimal("0")
    cashflows: list[tuple[OptionLeg, Decimal, Decimal]] = []
    strikes: set[Decimal] = set()
    for leg in legs:
        premium = premiums.get(leg.contract.instrument_token)
        if premium is None or premium <= 0:
            raise ValueError("Every leg requires a positive validated premium")
        quantity = Decimal(leg.contract.lot_size * leg.lots)
        sign = Decimal("-1") if leg.side is Side.BUY else Decimal("1")
        net_debit -= sign * premium * quantity
        cashflows.append((leg, premium, quantity))
        strikes.add(leg.contract.strike)

    def pnl(underlying: Decimal) -> Decimal:
        total = Decimal("0")
        for leg, premium, quantity in cashflows:
            if leg.contract.right.value == "CE":
                intrinsic = max(underlying - leg.contract.strike, Decimal("0"))
            else:
                intrinsic = max(leg.contract.strike - underlying, Decimal("0"))
            premium_sign = Decimal("1") if leg.side is Side.BUY else Decimal("-1")
            total += (intrinsic - premium) * quantity * premium_sign
        return total

    ordered = sorted(strikes)
    scenarios = {Decimal("0"), *ordered}
    if ordered:
        scenarios.add(ordered[-1] * Decimal("2") + Decimal("1"))
        for left, right in zip(ordered, ordered[1:], strict=False):
            scenarios.add((left + right) / Decimal("2"))
    scenario_pnl = tuple((spot, pnl(spot)) for spot in sorted(scenarios))
    values = [value for _, value in scenario_pnl]
    minimum = min(values)
    maximum = max(values)
    # Net short call quantity creates unbounded loss as the underlying rises.
    # Reject it rather than approximating risk at an arbitrary terminal price.
    call_slope = sum(
        (Decimal("1") if leg.side is Side.BUY else Decimal("-1"))
        * Decimal(leg.contract.lot_size * leg.lots)
        for leg in legs
        if leg.contract.right.value == "CE"
    )
    if call_slope < 0:
        raise ValueError("Structure has unbounded upside risk from net short calls")
    # A positive net long-call slope is bounded-loss but unbounded-profit.
    max_profit = None if call_slope > 0 else max(maximum, Decimal("0"))
    breakevens: list[Decimal] = []
    points = list(scenario_pnl)
    for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
        if y1 == 0:
            breakevens.append(x1)
        elif (y1 < 0 < y2) or (y2 < 0 < y1):
            breakevens.append(x1 + (x2 - x1) * (-y1) / (y2 - y1))
    if points[-1][1] == 0:
        breakevens.append(points[-1][0])
    return PayoffSummary(
        net_debit=net_debit,
        max_loss=max(-minimum, Decimal("0")),
        max_profit=max_profit,
        breakevens=tuple(sorted(set(breakevens))),
        scenario_pnl=tuple((spot, value - estimated_costs) for spot, value in scenario_pnl),
    )
