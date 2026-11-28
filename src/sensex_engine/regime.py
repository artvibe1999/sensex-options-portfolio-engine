"""Conservative multi-regime classifier for closed SENSEX index candles."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from .domain import Regime


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Candle timestamps must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("Candle prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Candle high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Candle low is inconsistent")


@dataclass(frozen=True)
class RegimeAnalysis:
    regime: Regime
    directional_bias: str
    adx: Decimal
    atr: Decimal
    atr_expansion_ratio: Decimal
    ema_fast: Decimal
    ema_slow: Decimal
    rationale: str


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    output = [current]
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
        output.append(current)
    return output


def _atr_and_adx(candles: list[Candle], period: int = 14) -> tuple[list[float], float]:
    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        high_move = float(current.high - previous.high)
        low_move = float(previous.low - current.low)
        true_ranges.append(max(
            float(current.high - current.low),
            abs(float(current.high - previous.close)),
            abs(float(current.low - previous.close)),
        ))
        plus_dm.append(high_move if high_move > low_move and high_move > 0 else 0.0)
        minus_dm.append(low_move if low_move > high_move and low_move > 0 else 0.0)

    atr = sum(true_ranges[:period]) / period
    plus = sum(plus_dm[:period]) / period
    minus = sum(minus_dm[:period]) / period
    atr_values: list[float] = []
    dx_values: list[float] = []
    for index in range(period - 1, len(true_ranges)):
        if index > period - 1:
            atr = (atr * (period - 1) + true_ranges[index]) / period
            plus = (plus * (period - 1) + plus_dm[index]) / period
            minus = (minus * (period - 1) + minus_dm[index]) / period
        atr_values.append(atr)
        plus_di = 100.0 * plus / atr if atr else 0.0
        minus_di = 100.0 * minus / atr if atr else 0.0
        denominator = plus_di + minus_di
        dx_values.append(
            100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0
        )

    adx = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        adx = (adx * (period - 1) + value) / period
    return atr_values, adx


def classify_regime(
    candles: list[Candle],
    *,
    as_of: datetime,
    minimum_bars: int = 55,
    max_last_bar_age_seconds: int = 900,
) -> RegimeAnalysis:
    """Classify using completed, ordered candles; ambiguity always maps to TRANSITION."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if len(candles) < minimum_bars or minimum_bars < 30:
        raise ValueError("Insufficient closed candles for regime analysis")
    timestamps = [candle.timestamp for candle in candles]
    if any(left >= right for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("Candles must be strictly chronological without duplicates")
    age = (as_of - timestamps[-1]).total_seconds()
    if age < -1 or age > max_last_bar_age_seconds:
        raise ValueError("Latest candle is stale or timestamp is in the future")

    closes = [float(candle.close) for candle in candles]
    fast = _ema(closes, 20)
    slow = _ema(closes, 50)
    atr_values, adx = _atr_and_adx(candles)
    if len(atr_values) < 20 or len(fast) < 6:
        raise ValueError("Insufficient indicator history")
    atr_now = atr_values[-1]
    atr_baseline = median(atr_values[-20:])
    expansion = atr_now / atr_baseline if atr_baseline else 0.0
    fast_now = fast[-1]
    fast_previous = fast[-6]
    slow_now = slow[-1]
    normalized_gap = (fast_now - slow_now) / atr_now if atr_now else 0.0
    slope = (fast_now - fast_previous) / atr_now if atr_now else 0.0

    if expansion >= 1.5:
        regime = Regime.VOLATILITY_EXPANSION
        bias = (
            "bullish" if normalized_gap > 0
            else "bearish" if normalized_gap < 0
            else "neutral"
        )
        rationale = (
            "ATR is at least 1.5x its recent median; directional trades "
            "require separate confirmation"
        )
    elif adx >= 20 and normalized_gap >= 0.15 and slope > 0.02:
        regime, bias = Regime.BULL_TREND, "bullish"
        rationale = "ADX, fast/slow EMA separation and fast EMA slope confirm bullish trend"
    elif adx >= 20 and normalized_gap <= -0.15 and slope < -0.02:
        regime, bias = Regime.BEAR_TREND, "bearish"
        rationale = "ADX, fast/slow EMA separation and fast EMA slope confirm bearish trend"
    elif adx < 16 and abs(normalized_gap) < 0.25:
        regime, bias = Regime.RANGE, "neutral"
        rationale = "Low ADX and compressed EMA separation indicate a range"
    else:
        regime, bias = Regime.TRANSITION, "neutral"
        rationale = "Trend and range evidence is mixed; suppress new strategy entries"

    return RegimeAnalysis(
        regime=regime,
        directional_bias=bias,
        adx=Decimal(str(adx)),
        atr=Decimal(str(atr_now)),
        atr_expansion_ratio=Decimal(str(expansion)),
        ema_fast=Decimal(str(fast_now)),
        ema_slow=Decimal(str(slow_now)),
        rationale=rationale,
    )
