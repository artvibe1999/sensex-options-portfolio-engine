"""Market-state snapshot built from completed 5-minute SENSEX bars.

Price structure, trend and momentum come from the SENSEX index. Volume and
VWAP come from the near-month SENSEX future because the index itself carries no
traded volume; when futures volume is missing, VWAP/volume are reported as
unavailable and every rule that needs them fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .domain import Regime
from .indicators import (
    IST,
    Bar,
    StructureState,
    bollinger_bandwidth,
    crossings,
    ema,
    macd_histogram,
    market_structure,
    median_or_none,
    percentile_rank,
    realized_volatility,
    session_vwap,
    sma,
    wilder_adx,
    wilder_atr,
    wilder_rsi,
)

BAR_MINUTES = 5
BARS_PER_YEAR = 75 * 252
SESSION_OPEN = time(9, 15)
MIN_BARS = 120


@dataclass(frozen=True)
class MarketState:
    as_of: datetime
    bar_time: datetime
    spot: float
    close: float
    prev_close: float
    prev_high: float
    prev_low: float
    ema9: float
    ema20: float
    ema21: float
    ema50: float
    ema200: float | None
    ema9_slope: float
    ema21_slope: float
    adx: float
    adx_prev: float
    adx_min_recent: float
    plus_di: float
    minus_di: float
    rsi: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    atr_prev: float
    atr_median: float
    bandwidth: float
    bandwidth_prev: float
    bandwidth_rank: float
    bandwidth_min_recent_rank: float
    bandwidth_mean: float
    structure: StructureState
    opening_range_high: float | None
    opening_range_low: float | None
    prev_day_high: float | None
    prev_day_low: float | None
    prev_day_close: float | None
    prev_week_high: float | None
    prev_week_low: float | None
    session_high: float
    session_low: float
    session_bars: int
    range_high: float
    range_low: float
    range_high_tests: int
    range_low_tests: int
    ema_crossings: int
    rv_annual: float | None
    rv_short: float | None
    # Futures-derived (None when unavailable)
    vwap: float | None
    future_close: float | None
    volume: float | None
    volume_avg: float | None
    vwap_crossings: int
    # Option-derived (filled by the engine after the chain is built)
    atm_iv_near: float | None = None
    atm_iv_far: float | None = None
    straddle_near: float | None = None
    days_to_near_expiry: float | None = None
    regime: Regime = Regime.UNKNOWN
    bias: str = "neutral"
    regime_reasons: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ helpers
    @property
    def vwap_available(self) -> bool:
        return self.vwap is not None and self.future_close is not None

    @property
    def above_vwap(self) -> bool:
        return self.vwap_available and self.future_close > self.vwap  # type: ignore[operator]

    @property
    def below_vwap(self) -> bool:
        return self.vwap_available and self.future_close < self.vwap  # type: ignore[operator]

    @property
    def vwap_distance_atr(self) -> float | None:
        if not self.vwap_available or self.atr <= 0:
            return None
        return abs(self.future_close - self.vwap) / self.atr  # type: ignore[operator]

    @property
    def volume_above_average(self) -> bool:
        return (
            self.volume is not None
            and self.volume_avg is not None
            and self.volume_avg > 0
            and self.volume > self.volume_avg
        )

    @property
    def adx_rising(self) -> bool:
        return self.adx > self.adx_prev

    def expected_move(self, iv: float | None = None, days: float | None = None) -> float | None:
        """One-sigma move: S * IV * sqrt(D/365)."""
        iv = self.atm_iv_near if iv is None else iv
        days = self.days_to_near_expiry if days is None else days
        if iv is None or days is None or iv <= 0 or days <= 0:
            return None
        return self.spot * iv * (days / 365.0) ** 0.5


def _last(values: list[float | None], back: int = 0) -> float | None:
    index = len(values) - 1 - back
    return values[index] if index >= 0 else None


def _require(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"Insufficient history for {name}")
    return value


def completed_bars(bars: list[Bar], now: datetime, minutes: int = BAR_MINUTES) -> list[Bar]:
    """Drop the forming bar and anything future-dated."""
    cutoff = now - timedelta(minutes=minutes)
    return [bar for bar in bars if bar.start <= cutoff]


def _session_levels(
    bars: list[Bar], today
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    sessions = sorted({bar.session for bar in bars if bar.session < today})
    if not sessions:
        return None, None, None, None, None
    prev = [bar for bar in bars if bar.session == sessions[-1]]
    pdh, pdl, pdc = max(b.high for b in prev), min(b.low for b in prev), prev[-1].close
    today_week = today.isocalendar()[:2]
    prior_week = [
        bar for bar in bars if bar.session < today and bar.session.isocalendar()[:2] != today_week
    ]
    if prior_week:
        last_week = prior_week[-1].session.isocalendar()[:2]
        week_bars = [b for b in prior_week if b.session.isocalendar()[:2] == last_week]
        return pdh, pdl, pdc, max(b.high for b in week_bars), min(b.low for b in week_bars)
    return pdh, pdl, pdc, None, None


def build_market_state(
    index_bars: list[Bar],
    *,
    now: datetime,
    spot: float,
    future_bars: list[Bar] | None = None,
    range_lookback: int = 24,
) -> MarketState:
    """Compute all indicators from completed bars; raise when history is insufficient."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    bars = completed_bars(index_bars, now)
    if len(bars) < MIN_BARS:
        raise ValueError(f"Need at least {MIN_BARS} completed 5-minute bars, have {len(bars)}")
    starts = [bar.start for bar in bars]
    if any(a >= b for a, b in zip(starts, starts[1:], strict=False)):
        raise ValueError("Bars must be strictly chronological")
    if (now - bars[-1].start).total_seconds() > 3 * BAR_MINUTES * 60:
        raise ValueError("Latest completed bar is stale")

    closes = [bar.close for bar in bars]
    e9, e20, e21, e50, e200 = (ema(closes, n) for n in (9, 20, 21, 50, 200))
    atr = wilder_atr(bars)
    dm = wilder_adx(bars)
    rsi = wilder_rsi(closes)
    hist = macd_histogram(closes)
    bw = bollinger_bandwidth(closes)
    bw_hist = [v for v in bw[-60:] if v is not None]
    bw_now = _require(_last(bw), "Bollinger bandwidth")
    recent_ranks = [percentile_rank(bw_hist, v) or 100.0 for v in bw_hist[-12:]]
    atr_now = _require(_last(atr), "ATR")
    today = bars[-1].session
    session = [bar for bar in bars if bar.session == today]
    pdh, pdl, pdc, pwh, pwl = _session_levels(bars, today)
    or_bars = [bar for bar in session if bar.start.astimezone(IST).time() < time(9, 30)]
    or_high = max(b.high for b in or_bars) if len(or_bars) >= 3 else None
    or_low = min(b.low for b in or_bars) if len(or_bars) >= 3 else None
    window = bars[-range_lookback:]
    range_high = max(b.high for b in window)
    range_low = min(b.low for b in window)
    tolerance = 0.25 * atr_now
    high_tests = _count_tests([b.high for b in window], range_high, tolerance)
    low_tests = _count_tests([b.low for b in window], range_low, tolerance)
    adx_values = [v for v in dm.adx[-12:] if v is not None]

    vwap = future_close = volume = volume_avg = None
    vwap_crossings = 0
    if future_bars:
        # Align to the index bar: BSE futures print extra post-close bars and may skip a
        # 5-minute window with no trades. A skipped/zero-volume bar has volume 0 (which
        # fails volume confirmation) but VWAP stays defined from cumulative session volume.
        fut = [b for b in completed_bars(future_bars, now) if b.start <= bars[-1].start]
        if fut and fut[-1].session == today and (bars[-1].start - fut[-1].start) <= timedelta(
            minutes=3 * BAR_MINUTES
        ):
            vw = session_vwap(fut)
            vols = [b.volume for b in fut]
            avg = sma(vols, 20)
            if _last(vw) is not None and _last(avg):
                vwap, future_close = _last(vw), fut[-1].close
                volume = vols[-1] if fut[-1].start == bars[-1].start else 0.0
                volume_avg = _last(avg)
                vwap_crossings = crossings([b.close for b in fut], vw, 20)

    state = MarketState(
        as_of=now,
        bar_time=bars[-1].start,
        spot=spot,
        close=closes[-1],
        prev_close=closes[-2],
        prev_high=bars[-2].high,
        prev_low=bars[-2].low,
        ema9=_require(_last(e9), "EMA9"),
        ema20=_require(_last(e20), "EMA20"),
        ema21=_require(_last(e21), "EMA21"),
        ema50=_require(_last(e50), "EMA50"),
        ema200=_last(e200),
        ema9_slope=(_require(_last(e9), "EMA9") - _require(_last(e9, 3), "EMA9")) / atr_now,
        ema21_slope=(_require(_last(e21), "EMA21") - _require(_last(e21, 3), "EMA21")) / atr_now,
        adx=_require(_last(dm.adx), "ADX"),
        adx_prev=_require(_last(dm.adx, 3), "ADX"),
        adx_min_recent=min(adx_values) if adx_values else 0.0,
        plus_di=_require(_last(dm.plus_di), "+DI"),
        minus_di=_require(_last(dm.minus_di), "-DI"),
        rsi=_require(_last(rsi), "RSI"),
        macd_hist=_require(_last(hist), "MACD"),
        macd_hist_prev=_require(_last(hist, 1), "MACD"),
        atr=atr_now,
        atr_prev=_require(_last(atr, 5), "ATR"),
        atr_median=_require(median_or_none(atr[-20:]), "ATR median"),
        bandwidth=bw_now,
        bandwidth_prev=_require(_last(bw, 1), "Bollinger bandwidth"),
        bandwidth_rank=percentile_rank(bw_hist, bw_now) or 100.0,
        bandwidth_min_recent_rank=min(recent_ranks) if recent_ranks else 100.0,
        bandwidth_mean=sum(bw_hist[-20:]) / len(bw_hist[-20:]),
        structure=market_structure(bars),
        opening_range_high=or_high,
        opening_range_low=or_low,
        prev_day_high=pdh,
        prev_day_low=pdl,
        prev_day_close=pdc,
        prev_week_high=pwh,
        prev_week_low=pwl,
        session_high=max(b.high for b in session),
        session_low=min(b.low for b in session),
        session_bars=len(session),
        range_high=range_high,
        range_low=range_low,
        range_high_tests=high_tests,
        range_low_tests=low_tests,
        ema_crossings=crossings(e9, e21, 20),
        rv_annual=realized_volatility(closes[-75:], BARS_PER_YEAR),
        rv_short=realized_volatility(closes[-25:], BARS_PER_YEAR),
        vwap=vwap,
        future_close=future_close,
        volume=volume,
        volume_avg=volume_avg,
        vwap_crossings=vwap_crossings,
    )
    regime, bias, reasons = classify(state)
    return _replace(state, regime=regime, bias=bias, regime_reasons=reasons)


def _replace(state: MarketState, **changes) -> MarketState:
    from dataclasses import replace

    return replace(state, **changes)


def with_options(
    state: MarketState,
    *,
    atm_iv_near: float | None,
    atm_iv_far: float | None,
    straddle_near: float | None,
    days_to_near_expiry: float | None,
) -> MarketState:
    updated = _replace(
        state,
        atm_iv_near=atm_iv_near,
        atm_iv_far=atm_iv_far,
        straddle_near=straddle_near,
        days_to_near_expiry=days_to_near_expiry,
    )
    regime, bias, reasons = classify(updated)
    return _replace(updated, regime=regime, bias=bias, regime_reasons=reasons)


def _count_tests(values: list[float], level: float, tolerance: float) -> int:
    """Separate touches of a level (consecutive touching bars count once)."""
    tests, touching = 0, False
    for value in values:
        near = abs(value - level) <= tolerance
        if near and not touching:
            tests += 1
        touching = near
    return tests


def classify(state: MarketState) -> tuple[Regime, str, tuple[str, ...]]:
    """Regime label for reporting and risk; strategies apply their own full rules."""
    reasons: list[str] = []
    bull_stack = state.ema9 > state.ema21 > state.ema50
    bear_stack = state.ema9 < state.ema21 < state.ema50
    expansion = state.atr_median > 0 and state.atr / state.atr_median >= 1.5
    compressed = state.bandwidth_rank <= 20 and state.atr < state.atr_median
    if expansion:
        reasons.append(f"ATR {state.atr / state.atr_median:.2f}x its 20-bar median")
        bias = "bullish" if state.plus_di > state.minus_di else "bearish"
        return Regime.VOLATILITY_EXPANSION, bias, tuple(reasons)
    if state.adx >= 20 and bull_stack and state.plus_di > state.minus_di:
        reasons.append(f"ADX {state.adx:.1f}, EMA9>21>50, +DI>-DI")
        return Regime.BULL_TREND, "bullish", tuple(reasons)
    if state.adx >= 20 and bear_stack and state.minus_di > state.plus_di:
        reasons.append(f"ADX {state.adx:.1f}, EMA9<21<50, -DI>+DI")
        return Regime.BEAR_TREND, "bearish", tuple(reasons)
    if state.adx < 20 and compressed:
        reasons.append(f"ADX {state.adx:.1f}, bandwidth rank {state.bandwidth_rank:.0f}")
        return Regime.VOLATILITY_COMPRESSION, "neutral", tuple(reasons)
    if state.adx < 20 and abs(state.ema9 - state.ema21) < 0.5 * state.atr:
        reasons.append(f"ADX {state.adx:.1f}, flat EMA9/21")
        return Regime.RANGE, "neutral", tuple(reasons)
    reasons.append("Mixed trend/range evidence")
    return Regime.TRANSITION, "neutral", tuple(reasons)


def is_regular_session(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and time(9, 15) <= local.time() <= time(15, 30)
