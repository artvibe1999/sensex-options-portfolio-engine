"""Deterministic, candidate-only defined-risk vertical strategy generation."""
from decimal import Decimal

from .domain import OptionLeg, Regime, Side, StrategyCandidate
from .option_chain import OptionAnalytics, OptionChainSnapshot
from .payoff import calculate_expiry_payoff


def _vertical(
    *,
    strategy_id: str,
    regime: Regime,
    long_leg: OptionAnalytics,
    short_leg: OptionAnalytics,
    lots: int,
    costs_per_lot: Decimal,
    rationale: str,
) -> StrategyCandidate | None:
    if lots <= 0 or costs_per_lot < 0:
        return None
    long_contract, short_contract = long_leg.contract, short_leg.contract
    if (
        long_contract.expiry != short_contract.expiry
        or long_contract.right is not short_contract.right
        or long_contract.lot_size != short_contract.lot_size
        or long_contract.strike == short_contract.strike
    ):
        return None
    legs = (
        OptionLeg(long_contract, Side.BUY, lots),
        OptionLeg(short_contract, Side.SELL, lots),
    )
    costs = costs_per_lot * lots
    try:
        payoff = calculate_expiry_payoff(
            legs,
            {
                long_contract.instrument_token: long_leg.mid_price,
                short_contract.instrument_token: short_leg.mid_price,
            },
            estimated_costs=costs,
        )
    except ValueError:
        return None
    width = abs(long_contract.strike - short_contract.strike)
    if payoff.max_loss <= 0 or payoff.max_loss > width * long_contract.lot_size * lots:
        return None
    if payoff.max_profit is not None and payoff.max_profit <= 0:
        return None
    return StrategyCandidate(
        strategy_id=strategy_id,
        regime=regime,
        legs=legs,
        max_loss=payoff.max_loss,
        max_profit=payoff.max_profit,
        net_debit=payoff.net_debit,
        estimated_costs=costs,
        data_timestamp=long_leg.quote.timestamp,
        is_defined_risk=True,
        rationale=rationale,
    )


def generate_directional_verticals(
    chain: OptionChainSnapshot,
    *,
    regime: Regime,
    lots: int,
    costs_per_lot: Decimal = Decimal("0"),
) -> tuple[StrategyCandidate, ...]:
    """Create conservative directional debit-spread candidates, never orders.

    Bull trend -> bull call debit spread; bear trend -> bear put debit spread.
    Range/transition/volatility-expansion/unknown regimes produce no candidate.
    The long leg is the closest available strike to spot; the short leg is the
    nearest strictly OTM strike with matching expiry/right/lot size.
    """
    if lots <= 0 or regime not in {Regime.BULL_TREND, Regime.BEAR_TREND}:
        return ()
    by_expiry: dict[object, list[OptionAnalytics]] = {}
    for item in chain.analytics:
        by_expiry.setdefault(item.contract.expiry, []).append(item)
    candidates: list[StrategyCandidate] = []
    for expiry_items in by_expiry.values():
        if regime is Regime.BULL_TREND:
            calls = sorted(
                (x for x in expiry_items if x.contract.right.value == "CE"),
                key=lambda x: x.contract.strike,
            )
            atm = min(
                calls,
                key=lambda x: abs(x.contract.strike - chain.underlying_price),
                default=None,
            )
            if atm is None:
                continue
            short = next((x for x in calls if x.contract.strike > chain.underlying_price), None)
            if short is None or short.contract.strike <= atm.contract.strike:
                continue
            candidate = _vertical(
                strategy_id="SENSEX_BULL_CALL_DEBIT",
                regime=regime,
                long_leg=atm,
                short_leg=short,
                lots=lots,
                costs_per_lot=costs_per_lot,
                rationale=(
                    "Bull-trend regime; defined-risk call debit vertical "
                    "using current listed strikes"
                ),
            )
        else:
            puts = sorted(
                (x for x in expiry_items if x.contract.right.value == "PE"),
                key=lambda x: x.contract.strike,
                reverse=True,
            )
            atm = min(
                puts,
                key=lambda x: abs(x.contract.strike - chain.underlying_price),
                default=None,
            )
            if atm is None:
                continue
            short = next((x for x in puts if x.contract.strike < chain.underlying_price), None)
            if short is None or short.contract.strike >= atm.contract.strike:
                continue
            candidate = _vertical(
                strategy_id="SENSEX_BEAR_PUT_DEBIT",
                regime=regime,
                long_leg=atm,
                short_leg=short,
                lots=lots,
                costs_per_lot=costs_per_lot,
                rationale=(
                    "Bear-trend regime; defined-risk put debit vertical "
                    "using current listed strikes"
                ),
            )
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)
