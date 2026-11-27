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
    """Create candidate-only directional debit and protected credit verticals.

    Bull trend -> bull call debit and bull put credit. Bear trend -> bear put
    debit and bear call credit. Range/transition/volatility-expansion/unknown
    regimes produce no candidates. All strikes are current listed contracts.
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
            if calls:
                atm = min(
                    calls,
                    key=lambda x: abs(x.contract.strike - chain.underlying_price),
                )
                short_call = next(
                    (x for x in calls if x.contract.strike > atm.contract.strike),
                    None,
                )
                if short_call is not None:
                    candidate = _vertical(
                        strategy_id="SENSEX_BULL_CALL_DEBIT",
                        regime=regime,
                        long_leg=atm,
                        short_leg=short_call,
                        lots=lots,
                        costs_per_lot=costs_per_lot,
                        rationale="Bull-trend; defined-risk call debit vertical",
                    )
                    if candidate is not None:
                        candidates.append(candidate)

            puts_below = sorted(
                (
                    x for x in expiry_items
                    if x.contract.right.value == "PE"
                    and x.contract.strike < chain.underlying_price
                ),
                key=lambda x: x.contract.strike,
                reverse=True,
            )
            if len(puts_below) >= 2:
                short_put, long_put = puts_below[0], puts_below[1]
                candidate = _vertical(
                    strategy_id="SENSEX_BULL_PUT_CREDIT",
                    regime=regime,
                    long_leg=long_put,
                    short_leg=short_put,
                    lots=lots,
                    costs_per_lot=costs_per_lot,
                    rationale="Bull-trend; defined-risk put credit with protective lower-strike put",
                )
                if candidate is not None:
                    candidates.append(candidate)

        else:
            puts = sorted(
                (x for x in expiry_items if x.contract.right.value == "PE"),
                key=lambda x: x.contract.strike,
                reverse=True,
            )
            if puts:
                atm = min(
                    puts,
                    key=lambda x: abs(x.contract.strike - chain.underlying_price),
                )
                short_put = next(
                    (x for x in puts if x.contract.strike < atm.contract.strike),
                    None,
                )
                if short_put is not None:
                    candidate = _vertical(
                        strategy_id="SENSEX_BEAR_PUT_DEBIT",
                        regime=regime,
                        long_leg=atm,
                        short_leg=short_put,
                        lots=lots,
                        costs_per_lot=costs_per_lot,
                        rationale="Bear-trend; defined-risk put debit vertical",
                    )
                    if candidate is not None:
                        candidates.append(candidate)

            calls_above = sorted(
                (
                    x for x in expiry_items
                    if x.contract.right.value == "CE"
                    and x.contract.strike > chain.underlying_price
                ),
                key=lambda x: x.contract.strike,
            )
            if len(calls_above) >= 2:
                short_call, long_call = calls_above[0], calls_above[1]
                candidate = _vertical(
                    strategy_id="SENSEX_BEAR_CALL_CREDIT",
                    regime=regime,
                    long_leg=long_call,
                    short_leg=short_call,
                    lots=lots,
                    costs_per_lot=costs_per_lot,
                    rationale="Bear-trend; defined-risk call credit with protective higher-strike call",
                )
                if candidate is not None:
                    candidates.append(candidate)

    return tuple(candidates)
