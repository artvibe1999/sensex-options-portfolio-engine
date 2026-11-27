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
    expiry = datetime(2026, 12, 31, tzinfo=UTC)
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
