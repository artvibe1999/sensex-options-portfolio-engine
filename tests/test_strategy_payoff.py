from datetime import UTC, datetime
from decimal import Decimal

from sensex_engine.domain import Contract, OptionLeg, Regime, Right, Side
from sensex_engine.market_data import Quote
from sensex_engine.option_chain import OptionAnalytics, OptionChainSnapshot
from sensex_engine.payoff import calculate_expiry_payoff
from sensex_engine.strategy import generate_directional_verticals


def _item(token: int, strike: str, right: Right, premium: str) -> OptionAnalytics:
    expiry = datetime(2026, 12, 31, tzinfo=UTC)
    contract = Contract(
        f"SX{strike}{right.value}", token, expiry, Decimal(strike), right, 20, Decimal("0.05")
    )
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    quote = Quote(token, now, Decimal(premium), Decimal(premium), Decimal(premium), 100, 100, 5000)
    return OptionAnalytics(
        contract, quote, Decimal(premium), Decimal("0.20"), Decimal("0.5"),
        Decimal("0.001"), Decimal("-2"), Decimal("10"), Decimal("5")
    )


def test_payoff_calculates_bull_call_debit_vertical_risk_and_reward():
    long = _item(1, "80000", Right.CE, "500")
    short = _item(2, "80500", Right.CE, "300")
    legs = (OptionLeg(long.contract, Side.BUY, 1), OptionLeg(short.contract, Side.SELL, 1))
    result = calculate_expiry_payoff(
        legs, {1: long.mid_price, 2: short.mid_price}
    )
    assert result.net_debit == Decimal("4000")
    assert result.max_loss == Decimal("4000")
    assert result.max_profit == Decimal("6000")
    assert result.breakevens


def test_bull_and_bear_regimes_generate_only_matching_vertical_candidates():
    call_atm = _item(1, "80000", Right.CE, "500")
    call_otm = _item(2, "80500", Right.CE, "300")
    put_atm = _item(3, "80000", Right.PE, "500")
    put_otm = _item(4, "79500", Right.PE, "300")
    chain = OptionChainSnapshot(
        Decimal("80000"), datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
        Decimal("0.06"), (call_atm, call_otm, put_atm, put_otm), (),
    )
    bull = generate_directional_verticals(chain, regime=Regime.BULL_TREND, lots=2)
    bear = generate_directional_verticals(chain, regime=Regime.BEAR_TREND, lots=1)
    assert bull and bull[0].strategy_id == "SENSEX_BULL_CALL_DEBIT"
    assert bull[0].max_loss == Decimal("8000")
    assert bear and bear[0].strategy_id == "SENSEX_BEAR_PUT_DEBIT"
    assert not generate_directional_verticals(chain, regime=Regime.RANGE, lots=1)
    assert not generate_directional_verticals(chain, regime=Regime.TRANSITION, lots=1)


def test_payoff_rejects_mixed_expiry_and_missing_premium():
    first = _item(1, "80000", Right.CE, "500")
    other = Contract(
        "OTHER", 3, datetime(2027, 1, 1, tzinfo=UTC), Decimal("80500"),
        Right.CE, 20, Decimal("0.05")
    )
    legs = (OptionLeg(first.contract, Side.BUY, 1), OptionLeg(other, Side.SELL, 1))
    try:
        calculate_expiry_payoff(legs, {1: first.mid_price, 3: Decimal("100")})
    except ValueError as exc:
        assert "single common expiry" in str(exc)
    else:
        raise AssertionError("mixed expiry must be rejected")


def test_payoff_rejects_net_short_call_unbounded_risk():
    from sensex_engine.payoff import calculate_expiry_payoff

    short_call = _item(9, "80000", Right.CE, "500")
    legs = (OptionLeg(short_call.contract, Side.SELL, 1),)
    try:
        calculate_expiry_payoff(legs, {9: short_call.mid_price})
    except ValueError as exc:
        assert "unbounded upside risk" in str(exc)
    else:
        raise AssertionError("net short call must fail defined-risk validation")


def test_directional_selector_uses_strike_beyond_selected_long_leg():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    items = (
        _item(11, "80200", Right.CE, "450"),
        _item(12, "80500", Right.CE, "250"),
        _item(13, "79500", Right.PE, "450"),
        _item(14, "79000", Right.PE, "250"),
    )
    chain = OptionChainSnapshot(
        Decimal("80180"), now, Decimal("0.06"), items, ()
    )
    bull = generate_directional_verticals(
        chain, regime=Regime.BULL_TREND, lots=1
    )
    assert bull
    assert bull[0].legs[0].contract.strike == Decimal("80200")
    assert bull[0].legs[1].contract.strike == Decimal("80500")

    put_chain = OptionChainSnapshot(
        Decimal("79820"), now, Decimal("0.06"), items, ()
    )
    bear = generate_directional_verticals(
        put_chain, regime=Regime.BEAR_TREND, lots=1
    )
    assert bear
    assert bear[0].legs[0].contract.strike == Decimal("79500")
    assert bear[0].legs[1].contract.strike == Decimal("79000")


def test_directional_regimes_offer_protected_credit_spread_candidates():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    items = (
        _item(21, "80000", Right.CE, "500"),
        _item(22, "80500", Right.CE, "300"),
        _item(23, "81000", Right.CE, "200"),
        _item(24, "80000", Right.PE, "500"),
        _item(25, "79500", Right.PE, "300"),
        _item(26, "79000", Right.PE, "200"),
    )
    chain = OptionChainSnapshot(
        Decimal("80000"), now, Decimal("0.06"), items, ()
    )
    bull = generate_directional_verticals(
        chain, regime=Regime.BULL_TREND, lots=1
    )
    bull_credit = next(x for x in bull if x.strategy_id == "SENSEX_BULL_PUT_CREDIT")
    assert bull_credit.net_debit < 0
    assert bull_credit.max_loss > 0
    assert bull_credit.max_profit is not None and bull_credit.max_profit > 0
    assert bull_credit.legs[0].contract.strike == Decimal("79000")
    assert bull_credit.legs[1].contract.strike == Decimal("79500")

    bear = generate_directional_verticals(
        chain, regime=Regime.BEAR_TREND, lots=1
    )
    bear_credit = next(x for x in bear if x.strategy_id == "SENSEX_BEAR_CALL_CREDIT")
    assert bear_credit.net_debit < 0
    assert bear_credit.max_loss > 0
    assert bear_credit.max_profit is not None and bear_credit.max_profit > 0
    assert bear_credit.legs[0].contract.strike == Decimal("81000")
    assert bear_credit.legs[1].contract.strike == Decimal("80500")


def test_spread_candidates_use_executable_side_prices_and_oldest_quote_time():
    from dataclasses import replace
    from datetime import timedelta

    long = _item(31, "80000", Right.CE, "500")
    short = _item(32, "80500", Right.CE, "300")
    long = replace(
        long,
        quote=replace(long.quote, bid=Decimal("490"), ask=Decimal("510")),
    )
    short = replace(
        short,
        quote=replace(
            short.quote,
            bid=Decimal("290"),
            ask=Decimal("310"),
            timestamp=short.quote.timestamp + timedelta(seconds=1),
        ),
    )
    chain = OptionChainSnapshot(
        Decimal("80000"), long.quote.timestamp, Decimal("0.06"),
        (long, short), (),
    )
    candidates = generate_directional_verticals(
        chain, regime=Regime.BULL_TREND, lots=1
    )
    debit = next(x for x in candidates if x.strategy_id == "SENSEX_BULL_CALL_DEBIT")
    assert debit.net_debit == Decimal("4400")
    assert debit.data_timestamp == long.quote.timestamp


def test_spread_candidate_rejected_when_leg_quotes_are_too_far_apart():
    from dataclasses import replace
    from datetime import timedelta

    long = _item(41, "80000", Right.CE, "500")
    short = _item(42, "80500", Right.CE, "300")
    short = replace(
        short,
        quote=replace(
            short.quote,
            timestamp=short.quote.timestamp + timedelta(seconds=3),
        ),
    )
    chain = OptionChainSnapshot(
        Decimal("80000"), long.quote.timestamp, Decimal("0.06"),
        (long, short), (),
    )
    candidates = generate_directional_verticals(
        chain, regime=Regime.BULL_TREND, lots=1
    )
    assert not candidates
