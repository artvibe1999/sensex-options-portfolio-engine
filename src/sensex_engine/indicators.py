"""Completed-candle technical indicators and market-structure primitives.

Every function is pure and operates on chronologically ordered completed bars.
Values that cannot be computed from the supplied history are returned as
``None`` (or omitted) rather than fabricated, so callers can fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import log, sqrt
from statistics import median, pstdev
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Bar:
    """One completed OHLCV bar. ``volume`` may be zero for an index."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("Bar timestamps must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("Bar prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("Bar high/low are inconsistent with open/close")
        if self.volume < 0:
            raise ValueError("Bar volume cannot be negative")

    @property
    def session(self) -> date:
        return self.start.astimezone(IST).date()


def ema(values: list[float], period: int) -> list[float | None]:
    """Standard recursive EMA (alpha = 2/(n+1)) seeded with the first SMA."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        out[index] = current
    return out


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        out[index] = sum(values[index - period + 1 : index + 1]) / period
    return out


def wilder_atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return out
    ranges = [bars[0].high - bars[0].low]
    for prev, cur in zip(bars, bars[1:], strict=False):
        ranges.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    current = sum(ranges[1 : period + 1]) / period
    out[period] = current
    for index in range(period + 1, len(bars)):
        current = (current * (period - 1) + ranges[index]) / period
        out[index] = current
    return out


@dataclass(frozen=True)
class DirectionalMovement:
    adx: list[float | None]
    plus_di: list[float | None]
    minus_di: list[float | None]


def wilder_adx(bars: list[Bar], period: int = 14) -> DirectionalMovement:
    """Wilder ADX/+DI/-DI. ADX is defined from bar ``2*period`` onwards."""
    n = len(bars)
    adx: list[float | None] = [None] * n
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    if n <= 2 * period:
        return DirectionalMovement(adx, plus_di, minus_di)
    tr, pdm, mdm = [0.0], [0.0], [0.0]
    for prev, cur in zip(bars, bars[1:], strict=False):
        up = cur.high - prev.high
        down = prev.low - cur.low
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    atr = sum(tr[1 : period + 1])
    plus = sum(pdm[1 : period + 1])
    minus = sum(mdm[1 : period + 1])
    dx: list[float] = []
    for index in range(period, n):
        if index > period:
            atr = atr - atr / period + tr[index]
            plus = plus - plus / period + pdm[index]
            minus = minus - minus / period + mdm[index]
        pdi = 100.0 * plus / atr if atr else 0.0
        mdi = 100.0 * minus / atr if atr else 0.0
        plus_di[index], minus_di[index] = pdi, mdi
        total = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / total if total else 0.0)
        if len(dx) == period:
            adx[index] = sum(dx) / period
        elif len(dx) > period:
            previous = adx[index - 1]
            assert previous is not None
            adx[index] = (previous * (period - 1) + dx[-1]) / period
    return DirectionalMovement(adx, plus_di, minus_di)


def wilder_rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / lo)

    out[period] = value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        out[index] = value(avg_gain, avg_loss)
    return out


def macd_histogram(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[float | None]:
    fast_line, slow_line = ema(values, fast), ema(values, slow)
    macd = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_line, slow_line, strict=True)
    ]
    defined = [m for m in macd if m is not None]
    signal_line = ema(defined, signal)
    out: list[float | None] = [None] * len(values)
    offset = len(values) - len(defined)
    for index, sig in enumerate(signal_line):
        if sig is not None:
            m = macd[offset + index]
            assert m is not None
            out[offset + index] = m - sig
    return out


def bollinger_bandwidth(
    values: list[float], period: int = 20, width: float = 2.0
) -> list[float | None]:
    """(upper - lower) / middle for a 20/2 Bollinger band."""
    out: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mid = sum(window) / period
        if mid > 0:
            out[index] = (2.0 * width * pstdev(window)) / mid
    return out


def session_vwap(bars: list[Bar]) -> list[float | None]:
    """Session-reset VWAP of typical price. ``None`` when the session has no volume."""
    out: list[float | None] = [None] * len(bars)
    session: date | None = None
    pv = vol = 0.0
    for index, bar in enumerate(bars):
        if bar.session != session:
            session, pv, vol = bar.session, 0.0, 0.0
        typical = (bar.high + bar.low + bar.close) / 3.0
        pv += typical * bar.volume
        vol += bar.volume
        out[index] = pv / vol if vol > 0 else None
    return out


def realized_volatility(closes: list[float], bars_per_year: float) -> float | None:
    """Annualised close-to-close realised volatility from log returns."""
    if len(closes) < 3 or any(c <= 0 for c in closes):
        return None
    returns = [log(b / a) for a, b in zip(closes, closes[1:], strict=False)]
    return pstdev(returns) * sqrt(bars_per_year)


def percentile_rank(history: list[float], value: float) -> float | None:
    if not history:
        return None
    return 100.0 * sum(1 for item in history if item <= value) / len(history)


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" | "low"


def swing_points(bars: list[Bar], left: int = 2, right: int = 2) -> list[Swing]:
    """Confirmed fractal swing highs/lows; the last ``right`` bars cannot confirm."""
    swings: list[Swing] = []
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        bar = bars[index]
        if bar.high == max(item.high for item in window) and all(
            item.high < bar.high for item in window if item is not bar
        ):
            swings.append(Swing(index, bar.high, "high"))
        if bar.low == min(item.low for item in window) and all(
            item.low > bar.low for item in window if item is not bar
        ):
            swings.append(Swing(index, bar.low, "low"))
    return swings


@dataclass(frozen=True)
class StructureState:
    last_highs: tuple[float, ...]
    last_lows: tuple[float, ...]
    higher_high: bool
    higher_low: bool
    lower_high: bool
    lower_low: bool
    bullish_bos: bool
    bearish_bos: bool
    bullish_choch: bool
    bearish_choch: bool

    @property
    def hh_hl(self) -> bool:
        return self.higher_high and self.higher_low

    @property
    def ll_lh(self) -> bool:
        return self.lower_low and self.lower_high


def market_structure(bars: list[Bar], *, bos_lookback: int = 3) -> StructureState:
    """HH/HL/LH/LL sequence plus break of structure in the latest bars.

    BOS: a recent completed close beyond the most recent confirmed swing.
    CHoCH: a BOS against the prevailing swing sequence (e.g. a bullish BOS
    while the last two swings formed LH+LL).
    """
    swings = swing_points(bars)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    hh = len(highs) >= 2 and highs[-1].price > highs[-2].price
    hl = len(lows) >= 2 and lows[-1].price > lows[-2].price
    lh = len(highs) >= 2 and highs[-1].price < highs[-2].price
    ll = len(lows) >= 2 and lows[-1].price < lows[-2].price
    recent = bars[-bos_lookback:] if bars else []
    bull_bos = (
        bool(highs)
        and any(b.close > highs[-1].price for b in recent)
        and highs[-1].index < len(bars) - 1
    )
    bear_bos = (
        bool(lows)
        and any(b.close < lows[-1].price for b in recent)
        and lows[-1].index < len(bars) - 1
    )
    return StructureState(
        last_highs=tuple(s.price for s in highs[-3:]),
        last_lows=tuple(s.price for s in lows[-3:]),
        higher_high=hh,
        higher_low=hl,
        lower_high=lh,
        lower_low=ll,
        bullish_bos=bull_bos,
        bearish_bos=bear_bos,
        bullish_choch=bull_bos and lh and ll,
        bearish_choch=bear_bos and hh and hl,
    )


def crossings(a: list[float | None], b: list[float | None], lookback: int) -> int:
    """Number of sign changes of (a-b) over the last ``lookback`` bars."""
    diffs = [
        x - y
        for x, y in zip(a[-lookback:], b[-lookback:], strict=False)
        if x is not None and y is not None and x != y
    ]
    return sum(1 for p, q in zip(diffs, diffs[1:], strict=False) if (p > 0) != (q > 0))


def median_or_none(values: list[float | None]) -> float | None:
    defined = [v for v in values if v is not None]
    return median(defined) if defined else None
