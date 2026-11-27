from datetime import UTC, datetime
from decimal import Decimal

from sensex_engine.domain import Contract, Regime, Right
from sensex_engine.market_data import Quote
from sensex_engine.option_chain import OptionAnalytics, OptionChainSnapshot
from sensex_engine.payoff import calculate_expiry_payoff
from sensex_engine.strategy import generate_directional_verticals
from sensex_engine.domain import OptionLeg, Side


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
    second = _item(2, "80500", Right.CE, "300")
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
