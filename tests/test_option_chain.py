from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sensex_engine.domain import Contract, Right
from sensex_engine.market_data import Quote
from sensex_engine.option_chain import build_option_chain


def _contract(token: int, right: Right) -> Contract:
    return Contract(
        tradingsymbol=f"SENSEX26DEC80000{right.value}",
        instrument_token=token,
        expiry=datetime(2026, 12, 31, tzinfo=UTC),
        strike=Decimal("80000"),
        right=right,
        lot_size=20,
        tick_size=Decimal("0.05"),
    )


def _quote(token: int, now: datetime, bid: str = "500", ask: str = "501") -> Quote:
    return Quote(token, now, Decimal("500.5"), Decimal(bid), Decimal(ask), 100, 120, 5000)


def test_chain_calculates_finite_call_and_put_greeks():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    contracts = [_contract(1, Right.CE), _contract(2, Right.PE)]
    quotes = {1: _quote(1, now), 2: _quote(2, now)}
    snapshot = build_option_chain(
        contracts, quotes, underlying_price=Decimal("80000"),
        underlying_timestamp=now, as_of=now, risk_free_rate=Decimal("0.06"),
    )
    assert len(snapshot.analytics) == 2
    call, put = snapshot.analytics
    assert Decimal("0") < call.implied_volatility < Decimal("5")
    assert Decimal("0") < call.delta < Decimal("1")
    assert Decimal("-1") < put.delta < Decimal("0")
    assert call.gamma > 0 and put.gamma > 0
    assert call.vega_per_vol_point > 0 and put.vega_per_vol_point > 0


def test_chain_rejects_missing_stale_and_invalid_quotes():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    contracts = [_contract(1, Right.CE), _contract(2, Right.PE)]
    quotes = {
        1: _quote(1, now - timedelta(seconds=30)),
        2: _quote(2, now, bid="0", ask="0"),
    }
    snapshot = build_option_chain(
        contracts, quotes, underlying_price=Decimal("80000"),
        underlying_timestamp=now, as_of=now, risk_free_rate=Decimal("0.06"),
    )
    assert not snapshot.analytics
    assert len(snapshot.rejected) == 2


def test_chain_fails_closed_on_stale_underlying_or_naive_clock():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    contract = _contract(1, Right.CE)
    try:
        build_option_chain(
            [contract], {1: _quote(1, now)}, underlying_price=Decimal("80000"),
            underlying_timestamp=now - timedelta(seconds=20), as_of=now,
            risk_free_rate=Decimal("0.06"),
        )
    except ValueError as exc:
        assert "Underlying index price is stale" in str(exc)
    else:
        raise AssertionError("stale spot must be rejected")

    try:
        build_option_chain(
            [contract], {1: _quote(1, now)}, underlying_price=Decimal("80000"),
            underlying_timestamp=now, as_of=datetime(2026, 11, 27),
            risk_free_rate=Decimal("0.06"),
        )
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive clock must be rejected")


def test_chain_rejects_quotes_off_contract_tick_grid():
    now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
    contract = _contract(1, Right.CE)
    quote = _quote(1, now, bid="500.01", ask="501.01")
    snapshot = build_option_chain(
        [contract], {1: quote}, underlying_price=Decimal("80000"),
        underlying_timestamp=now, as_of=now, risk_free_rate=Decimal("0.06"),
    )
    assert not snapshot.analytics
    assert snapshot.rejected[0].reason == "Quote prices violate contract tick size"
