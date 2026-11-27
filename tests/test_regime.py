from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sensex_engine.domain import Regime
from sensex_engine.regime import Candle, classify_regime


def _candles(*, trend: float, count: int = 80) -> list[Candle]:
    start = datetime(2026, 11, 27, 4, 0, tzinfo=UTC)
    result = []
    price = 80000.0
    for index in range(count):
        change = trend if trend else (0.15 if index % 2 else -0.15)
        opening = price
        price += change
        result.append(Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=Decimal(str(opening)),
            high=Decimal(str(max(opening, price) + 1)),
            low=Decimal(str(min(opening, price) - 1)),
            close=Decimal(str(price)),
        ))
    return result


def test_regime_classifier_identifies_strong_bull_and_bear_trends():
    for trend, expected, bias in (
        (8.0, Regime.BULL_TREND, "bullish"),
        (-8.0, Regime.BEAR_TREND, "bearish"),
    ):
        candles = _candles(trend=trend)
        result = classify_regime(candles, as_of=candles[-1].timestamp)
        assert result.regime is expected
        assert result.directional_bias == bias
        assert result.adx > 20


def test_regime_classifier_identifies_low_adx_range():
    candles = _candles(trend=0.0)
    result = classify_regime(candles, as_of=candles[-1].timestamp)
    assert result.regime is Regime.RANGE
    assert result.directional_bias == "neutral"


def test_regime_classifier_fails_closed_for_stale_or_disordered_data():
    candles = _candles(trend=3.0)
    with pytest.raises(ValueError, match="stale"):
        classify_regime(candles, as_of=candles[-1].timestamp + timedelta(hours=1))
    reordered = candles.copy()
    reordered[-1], reordered[-2] = reordered[-2], reordered[-1]
    with pytest.raises(ValueError, match="chronological"):
        classify_regime(reordered, as_of=candles[-1].timestamp)


def test_candle_rejects_inconsistent_ohlc():
    with pytest.raises(ValueError, match="high"):
        Candle(
            datetime.now(UTC), Decimal("10"), Decimal("9"),
            Decimal("8"), Decimal("10"),
        )
