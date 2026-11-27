"""SENSEX multi-leg strategy catalogue (8 defined-risk families).

Each family has:
* setup rules from the requirement (market structure + indicators), reported
  check-by-check so every no-trade decision is explainable;
* a strike optimiser that enumerates listed strikes inside the specified delta
  bands, prices each structure at executable bid/ask, applies the automatic
  rejection rules (spread, depth, Greeks, costs, opposing liquidity zone,
  reward/risk, risk budget) and ranks survivors with the weighted score
  0.25P + 0.20L + 0.15V + 0.15G + 0.10O + 0.10R + 0.05C (each 0-100).

Nothing here places orders. Thresholds are initial policy templates, not
validated optimal parameters, and no strategy is asserted to be profitable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from decimal import Decimal
from itertools import product

from .charges import ChargeSchedule, round_trip_estimate
from .domain import Contract, OptionLeg, Regime, Right, Side, StrategyCandidate
from .indicators import IST
from .market_state import MarketState
from .option_chain import OptionAnalytics, OptionChainSnapshot
from .payoff import calculate_expiry_payoff
from .scenario import ScenarioLeg, expiry_close, expiry_probability, stress, years_between

STRATEGY_VERSION = "catalogue-v1"

BULL_CALL = "SENSEX_BULL_CALL_DEBIT"
BEAR_PUT = "SENSEX_BEAR_PUT_DEBIT"
BULL_PUT = "SENSEX_BULL_PUT_CREDIT"
BEAR_CALL = "SENSEX_BEAR_CALL_CREDIT"
IRON_CONDOR = "SENSEX_IRON_CONDOR"
IRON_BUTTERFLY = "SENSEX_IRON_BUTTERFLY"
CALENDAR = "SENSEX_CALENDAR_DIAGONAL"
LONG_IRON_BUTTERFLY = "SENSEX_LONG_IRON_BUTTERFLY"

# Requirement section 9: arbitration priority (not a profitability ranking).
PRIORITY = (
    BULL_CALL,
    BEAR_PUT,
    BULL_PUT,
    BEAR_CALL,
    IRON_CONDOR,
    IRON_BUTTERFLY,
    CALENDAR,
    LONG_IRON_BUTTERFLY,
)
DIRECTION = {
    BULL_CALL: "bullish",
    BULL_PUT: "bullish",
    BEAR_PUT: "bearish",
    BEAR_CALL: "bearish",
    IRON_CONDOR: "neutral",
    IRON_BUTTERFLY: "neutral",
    CALENDAR: "neutral",
    LONG_IRON_BUTTERFLY: "volatility",
}
# Entry windows (IST). Opening 15 minutes are assessment only; no entries after 14:30.
WINDOWS = {
    BULL_CALL: (time(9, 30), time(14, 30)),
    BEAR_PUT: (time(9, 30), time(14, 30)),
    BULL_PUT: (time(9, 45), time(14, 30)),
    BEAR_CALL: (time(9, 45), time(14, 30)),
    IRON_CONDOR: (time(10, 30), time(14, 0)),
    IRON_BUTTERFLY: (time(10, 30), time(14, 0)),
    CALENDAR: (time(10, 30), time(14, 0)),
    LONG_IRON_BUTTERFLY: (time(9, 45), time(14, 0)),
}
WEIGHTS = {"P": 0.25, "L": 0.20, "V": 0.15, "G": 0.15, "O": 0.10, "R": 0.10, "C": 0.05}


# --------------------------------------------------------------------- types
@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ExitPlan:
    """Strategy-specific management template (requirement section 3 exits)."""

    profit_target_pct: float  # of max profit (debit/credit) or of debit gain (long vol)
    profit_basis: str  # "max_profit" | "debit"
    stop_loss_pct: float  # of max loss
    short_delta_limit: float | None  # exit when any short leg |delta| exceeds this
    invalidate_below: float | None  # underlying close below -> thesis invalid
    invalidate_above: float | None  # underlying close above -> thesis invalid
    time_stop_bars: int | None = None  # long-vol: exit if move does not develop
    alignment: str | None = None  # "bullish" | "bearish": exit on VWAP+EMA reversal
    range_low: float | None = None  # neutral: exit on close outside range
    range_high: float | None = None


@dataclass(frozen=True)
class Proposal:
    strategy_id: str
    regime: Regime
    legs: tuple[tuple[OptionAnalytics, Side], ...]
    prices: tuple[Decimal, ...]  # executable per-unit entry price per leg
    lot_size: int
    net_debit_per_lot: Decimal  # negative = credit
    max_loss_per_lot: Decimal
    max_profit_per_lot: Decimal | None
    costs_per_lot: Decimal
    breakevens: tuple[Decimal, ...]
    pop: float
    expected_value_per_lot: float
    stress_worst_per_lot: float
    score: float
    components: dict[str, float]
    exit_plan: ExitPlan
    rationale: str
    data_timestamp: datetime
    multi_expiry: bool = False

    @property
    def risk_per_lot(self) -> Decimal:
        return self.max_loss_per_lot + self.costs_per_lot

    def describe_legs(self) -> str:
        return ", ".join(
            f"{side.value} {a.contract.tradingsymbol}@{price} (d={float(a.delta):+.2f})"
            for (a, side), price in zip(self.legs, self.prices, strict=True)
        )

    def to_candidate(self, lots: int) -> StrategyCandidate:
        legs = tuple(OptionLeg(a.contract, side, lots) for a, side in self.legs)
        return StrategyCandidate(
            strategy_id=self.strategy_id,
            regime=self.regime,
            legs=legs,
            max_loss=self.max_loss_per_lot * lots,
            max_profit=None if self.max_profit_per_lot is None else self.max_profit_per_lot * lots,
            net_debit=self.net_debit_per_lot * lots,
            estimated_costs=self.costs_per_lot * lots,
            data_timestamp=self.data_timestamp,
            is_defined_risk=True,
            rationale=self.rationale,
        )


@dataclass(frozen=True)
class Evaluation:
    strategy_id: str
    priority: int
    checks: tuple[Check, ...]
    proposal: Proposal | None
    rejection: str
    rejected_structures: int = 0

    @property
    def setup_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def eligible(self) -> bool:
        return self.setup_passed and self.proposal is not None

    def summary(self) -> str:
        if self.eligible:
            p = self.proposal
            assert p is not None
            return (
                f"{self.strategy_id}: ELIGIBLE score={p.score:.1f} risk/lot=Rs{p.risk_per_lot:,.0f}"
                f" | {p.describe_legs()}"
            )
        failed = [c for c in self.checks if not c.passed]
        reason = self.rejection or "; ".join(f"{c.name} ({c.detail})" for c in failed[:4])
        return f"{self.strategy_id}: no trade - {reason}"


@dataclass(frozen=True)
class StrategyConfig:
    max_spread_pct: float = 0.05
    max_spread_abs: float = 1.0  # Rs; cheap OTM wings quote a few ticks wide
    min_score: float = 55.0
    max_cost_to_profit: float = 0.40  # reject if round-trip costs > 40% of max profit
    # (width - debit)/debit. Under fair pricing debit/width ~ mean delta of the two legs, so
    # the requirement's 0.55-0.65 / 0.25-0.35 bands give ~1.0-1.6; 1.3 excludes most of the
    # band once bid/ask is paid. 1.1 keeps "positive reward/risk after costs" meaningful.
    min_debit_rr: float = 1.1
    # Fair credit/width ~ probability the short finishes ITM; for the 0.20-0.30 / 0.05-0.15
    # delta bands that is ~0.15-0.25, so 0.20 would reject the requirement's own bands.
    min_credit_to_width: float = 0.15  # credit verticals
    min_condor_credit_to_width: float = 0.12  # iron condor (credit vs the wider wing)
    min_ib_credit_to_width: float = 0.35  # iron butterfly
    min_long_ib_rr: float = 0.5  # long iron butterfly max profit / debit
    delta_tolerance: float = 0.05  # widen bands once if no listed strike fits
    ib_risk_fraction: Decimal = Decimal("0.5")  # IB sized smaller (concentrated gamma)
    rate: float = 0.065
    charges: ChargeSchedule = field(default_factory=ChargeSchedule)


@dataclass(frozen=True)
class MarketContext:
    state: MarketState
    near: OptionChainSnapshot  # nearest non-expiring expiry
    far: OptionChainSnapshot | None
    oi: dict[int, int]  # instrument_token -> open interest
    now: datetime


# ------------------------------------------------------------- setup rules
def _c(name: str, passed: bool, detail: str = "") -> Check:
    return Check(name, bool(passed), detail)


def _window(strategy_id: str, now: datetime) -> Check:
    start, end = WINDOWS[strategy_id]
    local = now.astimezone(IST).time()
    return _c("session window", start <= local <= end, f"{start:%H:%M}-{end:%H:%M} IST")


def _vwap(state: MarketState, above: bool) -> Check:
    if not state.vwap_available:
        return _c("VWAP", False, "futures volume unavailable; VWAP not fabricated")
    ok = state.above_vwap if above else state.below_vwap
    return _c("price vs VWAP", ok, f"fut {state.future_close:.1f} vs VWAP {state.vwap:.1f}")


def _volume(state: MarketState) -> Check:
    if state.volume is None:
        return _c("volume > 20-bar avg", False, "futures volume unavailable")
    return _c(
        "volume > 20-bar avg",
        state.volume_above_average,
        f"{state.volume:.0f} vs {state.volume_avg:.0f}",
    )


def setup_bull_call(s: MarketState, now: datetime) -> list[Check]:
    st = s.structure
    breakout = st.bullish_bos or (
        s.opening_range_high is not None
        and s.close > s.opening_range_high
        and s.prev_close <= s.opening_range_high
    )
    return [
        _window(BULL_CALL, now),
        _c("HH-HL structure", st.hh_hl, f"highs {st.last_highs} lows {st.last_lows}"),
        _c("close above prior swing high / resistance", breakout, "BOS or opening-range breakout"),
        _c(
            "EMA9 > EMA21 > EMA50",
            s.ema9 > s.ema21 > s.ema50,
            f"{s.ema9:.1f}/{s.ema21:.1f}/{s.ema50:.1f}",
        ),
        _vwap(s, above=True),
        _c(
            "ADX >= 20 and rising",
            s.adx >= 20 and s.adx_rising,
            f"ADX {s.adx:.1f} prev {s.adx_prev:.1f}",
        ),
        _c("+DI > -DI", s.plus_di > s.minus_di, f"{s.plus_di:.1f}/{s.minus_di:.1f}"),
        _c("RSI > 55", s.rsi > 55, f"{s.rsi:.1f}"),
        _c("MACD histogram > 0", s.macd_hist > 0, f"{s.macd_hist:.2f}"),
        _volume(s),
        _c(
            "ATR supports expansion",
            s.atr >= s.atr_median,
            f"{s.atr:.1f} vs median {s.atr_median:.1f}",
        ),
    ]


def setup_bear_put(s: MarketState, now: datetime) -> list[Check]:
    st = s.structure
    breakdown = st.bearish_bos or (
        s.opening_range_low is not None
        and s.close < s.opening_range_low
        and s.prev_close >= s.opening_range_low
    )
    return [
        _window(BEAR_PUT, now),
        _c("LL-LH structure", st.ll_lh, f"highs {st.last_highs} lows {st.last_lows}"),
        _c("close below prior swing low / support", breakdown, "BOS or opening-range breakdown"),
        _c(
            "EMA9 < EMA21 < EMA50",
            s.ema9 < s.ema21 < s.ema50,
            f"{s.ema9:.1f}/{s.ema21:.1f}/{s.ema50:.1f}",
        ),
        _vwap(s, above=False),
        _c(
            "ADX >= 20 and rising",
            s.adx >= 20 and s.adx_rising,
            f"ADX {s.adx:.1f} prev {s.adx_prev:.1f}",
        ),
        _c("-DI > +DI", s.minus_di > s.plus_di, f"{s.minus_di:.1f}/{s.plus_di:.1f}"),
        _c("RSI < 45", s.rsi < 45, f"{s.rsi:.1f}"),
        _c("MACD histogram < 0", s.macd_hist < 0, f"{s.macd_hist:.2f}"),
        _volume(s),
        _c(
            "ATR supports expansion",
            s.atr >= s.atr_median,
            f"{s.atr:.1f} vs median {s.atr_median:.1f}",
        ),
    ]


def _support(s: MarketState) -> float | None:
    return s.structure.last_lows[-1] if s.structure.last_lows else None


def _resistance(s: MarketState) -> float | None:
    return s.structure.last_highs[-1] if s.structure.last_highs else None


def setup_bull_put(s: MarketState, now: datetime) -> list[Check]:
    support = _support(s)
    zone = [z for z in (support, s.vwap, s.ema20) if z is not None]
    tested = any(s.prev_low <= z + 0.5 * s.atr for z in zone) if zone else False
    return [
        _window(BULL_PUT, now),
        _c("confirmed higher low", s.structure.higher_low, f"lows {s.structure.last_lows}"),
        _c("support zone tested", tested, "prior bar low within 0.5 ATR of swing low/VWAP/EMA20"),
        _c(
            "bullish close above previous high",
            s.close > s.prev_high,
            f"{s.close:.1f} vs {s.prev_high:.1f}",
        ),
        _c("EMA20 > EMA50", s.ema20 > s.ema50, f"{s.ema20:.1f}/{s.ema50:.1f}"),
        _vwap(s, above=True),
        _c("RSI 50-65", 50 <= s.rsi <= 65, f"{s.rsi:.1f}"),
        _c("ADX > 18", s.adx > 18, f"{s.adx:.1f}"),
        _c("no bearish BOS", not s.structure.bearish_bos, ""),
    ]


def setup_bear_call(s: MarketState, now: datetime) -> list[Check]:
    resistance = _resistance(s)
    zone = [z for z in (resistance, s.vwap, s.ema20) if z is not None]
    tested = any(s.prev_high >= z - 0.5 * s.atr for z in zone) if zone else False
    return [
        _window(BEAR_CALL, now),
        _c("confirmed lower high", s.structure.lower_high, f"highs {s.structure.last_highs}"),
        _c(
            "resistance tested and failed",
            tested,
            "prior bar high within 0.5 ATR of swing high/VWAP/EMA20",
        ),
        _c(
            "bearish close below previous low",
            s.close < s.prev_low,
            f"{s.close:.1f} vs {s.prev_low:.1f}",
        ),
        _c("EMA9 < EMA21", s.ema9 < s.ema21, f"{s.ema9:.1f}/{s.ema21:.1f}"),
        _vwap(s, above=False),
        _c("RSI < 50", s.rsi < 50, f"{s.rsi:.1f}"),
        _c("ADX > 18", s.adx > 18, f"{s.adx:.1f}"),
        _c("MACD bearish", s.macd_hist < 0, f"{s.macd_hist:.2f}"),
        _c("no bullish BOS", not s.structure.bullish_bos, ""),
    ]


def _iv_vs_rv(s: MarketState, short_vol: bool) -> Check:
    if s.atm_iv_near is None or s.rv_annual is None:
        return _c("IV vs realised vol", False, "IV or realised volatility unavailable")
    ok = s.atm_iv_near > s.rv_annual if short_vol else s.atm_iv_near < s.rv_annual * 1.5
    return _c("IV vs realised vol", ok, f"IV {s.atm_iv_near:.3f} vs RV {s.rv_annual:.3f}")


def setup_iron_condor(s: MarketState, now: datetime) -> list[Check]:
    flat = abs(s.ema9 - s.ema21) < 0.15 * s.atr or s.ema_crossings >= 2
    return [
        _window(IRON_CONDOR, now),
        _c("ADX < 20", s.adx < 20, f"{s.adx:.1f}"),
        _c(
            "EMA9/21 flat or crossing",
            flat,
            f"gap {abs(s.ema9 - s.ema21):.1f}, crosses {s.ema_crossings}",
        ),
        _c(
            "price oscillates around VWAP",
            s.vwap_available and s.vwap_crossings >= 2,
            f"crossings {s.vwap_crossings}" if s.vwap_available else "VWAP unavailable",
        ),
        _c(
            "Bollinger bands narrowing",
            s.bandwidth < s.bandwidth_mean,
            f"{s.bandwidth:.5f} vs mean {s.bandwidth_mean:.5f}",
        ),
        _c("RSI 40-60", 40 <= s.rsi <= 60, f"{s.rsi:.1f}"),
        _c("ATR contracting", s.atr < s.atr_median, f"{s.atr:.1f} vs {s.atr_median:.1f}"),
        _iv_vs_rv(s, short_vol=True),
        _c(
            ">= 2 tests of each range boundary",
            s.range_high_tests >= 2 and s.range_low_tests >= 2,
            f"high {s.range_high_tests}, low {s.range_low_tests}",
        ),
        _c("no developing trend structure", not (s.structure.hh_hl or s.structure.ll_lh), ""),
    ]


def setup_iron_butterfly(s: MarketState, now: datetime) -> list[Check]:
    dist = s.vwap_distance_atr
    return [
        _window(IRON_BUTTERFLY, now),
        _c("ADX < 18", s.adx < 18, f"{s.adx:.1f}"),
        _c("RSI 45-55", 45 <= s.rsi <= 55, f"{s.rsi:.1f}"),
        _c(
            "price near VWAP",
            dist is not None and dist <= 0.3,
            f"{dist:.2f} ATR" if dist is not None else "VWAP unavailable",
        ),
        _c("repeated mean reversion", s.vwap_crossings >= 3, f"VWAP crossings {s.vwap_crossings}"),
        _c("Bollinger compressed", s.bandwidth_rank <= 30, f"rank {s.bandwidth_rank:.0f}"),
        _c("ATR declining", s.atr < s.atr_prev, f"{s.atr:.1f} vs {s.atr_prev:.1f}"),
        _iv_vs_rv(s, short_vol=True),
        _c("no HH-HL / LL-LH trend", not (s.structure.hh_hl or s.structure.ll_lh), ""),
    ]


def setup_calendar(s: MarketState, now: datetime) -> list[Check]:
    ok_term = (
        s.atm_iv_near is not None
        and s.atm_iv_far is not None
        and s.atm_iv_near >= s.atm_iv_far * 1.05
    )
    low_rv = s.rv_short is not None and s.rv_annual is not None and s.rv_short <= s.rv_annual
    return [
        _window(CALENDAR, now),
        _c("expected consolidation (ADX < 20)", s.adx < 20, f"{s.adx:.1f}"),
        _c("low near-term realised vol", low_rv, f"RV short {s.rv_short} vs RV {s.rv_annual}"),
        _c("front IV >= 1.05 x back IV", ok_term, f"near {s.atm_iv_near} far {s.atm_iv_far}"),
    ]


def setup_long_iron_butterfly(s: MarketState, now: datetime) -> list[Check]:
    breakout = (
        s.opening_range_high is not None
        and s.opening_range_low is not None
        and (s.close > s.opening_range_high or s.close < s.opening_range_low)
    )
    return [
        _window(LONG_IRON_BUTTERFLY, now),
        _c(
            "recent Bollinger squeeze",
            s.bandwidth_min_recent_rank <= 20,
            f"min recent rank {s.bandwidth_min_recent_rank:.0f}",
        ),
        _c(
            "bandwidth now expanding",
            s.bandwidth > s.bandwidth_prev * 1.05,
            f"{s.bandwidth:.5f} vs {s.bandwidth_prev:.5f}",
        ),
        _c("ATR expansion", s.atr > s.atr_prev, f"{s.atr:.1f} vs {s.atr_prev:.1f}"),
        _c(
            "ADX turning up from low",
            s.adx_rising and s.adx_min_recent < 20,
            f"ADX {s.adx:.1f}, recent min {s.adx_min_recent:.1f}",
        ),
        _volume(s),
        _c("opening range broken", breakout, f"OR {s.opening_range_low}-{s.opening_range_high}"),
        _iv_vs_rv(s, short_vol=False),
    ]


SETUPS: dict[str, Callable[[MarketState, datetime], list[Check]]] = {
    BULL_CALL: setup_bull_call,
    BEAR_PUT: setup_bear_put,
    BULL_PUT: setup_bull_put,
    BEAR_CALL: setup_bear_call,
    IRON_CONDOR: setup_iron_condor,
    IRON_BUTTERFLY: setup_iron_butterfly,
    CALENDAR: setup_calendar,
    LONG_IRON_BUTTERFLY: setup_long_iron_butterfly,
}
REGIME_FOR = {
    BULL_CALL: Regime.BULL_TREND,
    BULL_PUT: Regime.BULL_TREND,
    BEAR_PUT: Regime.BEAR_TREND,
    BEAR_CALL: Regime.BEAR_TREND,
    IRON_CONDOR: Regime.RANGE,
    IRON_BUTTERFLY: Regime.RANGE,
    CALENDAR: Regime.RANGE,
    LONG_IRON_BUTTERFLY: Regime.VOLATILITY_EXPANSION,
}


# ----------------------------------------------------------- strike engine
def _abs_delta(a: OptionAnalytics) -> float:
    return abs(float(a.delta))


def _in_band(
    items: Iterable[OptionAnalytics], lo: float, hi: float, tol: float
) -> list[OptionAnalytics]:
    items = list(items)
    strict = [a for a in items if lo <= _abs_delta(a) <= hi]
    return strict or [a for a in items if lo - tol <= _abs_delta(a) <= hi + tol]


def _delta_range(items: Iterable[OptionAnalytics], lo: float, hi: float) -> list[OptionAnalytics]:
    """Strikes with lo <= |delta| < hi (capital-fit pass: narrower protective/cap legs)."""
    return [a for a in items if lo <= _abs_delta(a) < hi]


def _by_right(chain: OptionChainSnapshot, right: Right) -> list[OptionAnalytics]:
    return sorted(
        (a for a in chain.analytics if a.contract.right is right), key=lambda a: a.contract.strike
    )


def _exec_price(a: OptionAnalytics, side: Side) -> Decimal:
    return a.quote.ask if side is Side.BUY else a.quote.bid


def _leg_liquidity(a: OptionAnalytics, side: Side, cfg: StrategyConfig) -> tuple[bool, float, str]:
    q = a.quote
    mid = float((q.bid + q.ask) / 2)
    spread = float(q.ask - q.bid)
    if mid <= 0:
        return False, 0.0, "no mid price"
    rel = spread / mid
    if rel > cfg.max_spread_pct and spread > cfg.max_spread_abs:
        return False, 0.0, f"{a.contract.tradingsymbol} spread {rel:.1%}"
    depth = q.ask_quantity if side is Side.BUY else q.bid_quantity
    if depth < a.contract.lot_size:
        return False, 0.0, f"{a.contract.tradingsymbol} depth {depth} < 1 lot"
    depth_score = min(1.0, depth / (a.contract.lot_size * 5))
    spread_score = max(0.0, 1.0 - rel / cfg.max_spread_pct)
    return True, 100.0 * (0.6 * spread_score + 0.4 * depth_score), ""


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


@dataclass(frozen=True)
class _Build:
    proposal: Proposal | None
    reason: str


def _price_structure(
    strategy_id: str,
    legs: list[tuple[OptionAnalytics, Side]],
    ctx: MarketContext,
    cfg: StrategyConfig,
    *,
    risk_budget: Decimal,
    exit_plan: ExitPlan,
    rationale: str,
    reject_rule: Callable[[Decimal, Decimal | None, Decimal], str] | None = None,
    multi_expiry: bool = False,
) -> _Build:
    lot = legs[0][0].contract.lot_size
    if any(a.contract.lot_size != lot for a, _ in legs):
        return _Build(None, "legs have different lot sizes")
    if len({a.contract.instrument_token for a, _ in legs}) != len(legs):
        return _Build(None, "duplicate legs")
    liquidity = []
    for a, side in legs:
        ok, score, why = _leg_liquidity(a, side, cfg)
        if not ok:
            return _Build(None, why)
        liquidity.append(score)
    stamps = [a.quote.timestamp for a, _ in legs]
    if (max(stamps) - min(stamps)).total_seconds() > 10:
        return _Build(None, "leg quotes are not synchronised")
    prices = [_exec_price(a, side) for a, side in legs]
    if any(p <= 0 for p in prices):
        return _Build(None, "non-positive executable price")
    option_legs = tuple(OptionLeg(a.contract, side, 1) for a, side in legs)
    costs = round_trip_estimate(
        [(side.value, price, lot) for (_, side), price in zip(legs, prices, strict=True)],
        cfg.charges,
    )
    state = ctx.state
    sc_legs = [
        ScenarioLeg(
            a.contract.right.value,
            float(a.contract.strike),
            a.contract.expiry,
            side.value,
            lot,
            float(price),
            float(a.implied_volatility),
        )
        for (a, side), price in zip(legs, prices, strict=True)
    ]
    horizon = datetime.combine(ctx.now.astimezone(IST).date(), time(15, 10), tzinfo=IST)
    stress_report = stress(
        sc_legs, spot=state.spot, at=ctx.now, horizon=horizon, rate=cfg.rate, costs=float(costs)
    )
    if multi_expiry:
        net_debit = sum(
            (p if side is Side.BUY else -p) * lot for (_, side), p in zip(legs, prices, strict=True)
        )
        if net_debit <= 0:
            return _Build(None, "calendar/diagonal must be a net debit")
        max_loss = Decimal(str(net_debit))
        max_profit = Decimal(str(round(max(stress_report.best_pnl + float(costs), 0.0), 2)))
        breakevens: tuple[Decimal, ...] = ()
        pop = sum(1 for g in stress_report.grid if g[2] > 0) / len(stress_report.grid)
        ev = sum(g[2] for g in stress_report.grid if g[1] == 0.0) / len(
            [g for g in stress_report.grid if g[1] == 0.0]
        )
    else:
        try:
            payoff = calculate_expiry_payoff(
                option_legs,
                {a.contract.instrument_token: p for (a, _), p in zip(legs, prices, strict=True)},
            )
        except ValueError as exc:
            return _Build(None, f"payoff rejected: {exc}")
        max_loss, max_profit, net_debit = payoff.max_loss, payoff.max_profit, payoff.net_debit
        breakevens = payoff.breakevens
        if max_loss <= 0:
            return _Build(None, "max loss is not positive (mispriced quotes)")
        if max_profit is not None and max_profit <= 0:
            return _Build(None, "no positive payoff after executable prices")

        def terminal(s: float) -> float:
            total = 0.0
            for leg in sc_legs:
                intrinsic = (
                    max(s - leg.strike, 0.0) if leg.right == "CE" else max(leg.strike - s, 0.0)
                )
                total += leg.sign * leg.quantity * (intrinsic - leg.entry_price)
            return total

        expiry = legs[0][0].contract.expiry
        years = years_between(ctx.now, expiry_close(expiry))
        iv = sum(float(a.implied_volatility) for a, _ in legs) / len(legs)
        prob = expiry_probability(terminal, spot=state.spot, iv=iv, years=years, costs=float(costs))
        pop, ev = prob.pop, prob.expected_value

    # Hard rejection rules (requirement 5E).
    if max_profit is not None and costs > max_profit * Decimal(str(cfg.max_cost_to_profit)):
        return _Build(None, f"costs Rs{costs} exceed {cfg.max_cost_to_profit:.0%} of max profit")
    if reject_rule is not None:
        why = reject_rule(max_loss, max_profit, Decimal(str(net_debit)))
        if why:
            return _Build(None, why)
    if max_loss + costs > risk_budget:
        return _Build(None, f"1-lot risk Rs{max_loss + costs:,.0f} > budget Rs{risk_budget:,.0f}")

    # Score components (0-100).
    direction = DIRECTION[strategy_id]
    net_delta = sum(float(a.delta) * (1 if side is Side.BUY else -1) for a, side in legs)
    gamma = sum(float(a.gamma) * (1 if side is Side.BUY else -1) for a, side in legs)
    if direction == "bullish":
        g = _clamp(100 * net_delta / 0.30) if net_delta > 0 else 0.0
    elif direction == "bearish":
        g = _clamp(-100 * net_delta / 0.30) if net_delta < 0 else 0.0
    else:
        g = _clamp(100 * (1 - abs(net_delta) / 0.20))
    if direction in {"neutral"} and gamma < 0:
        g = _clamp(g - min(30.0, abs(gamma) * state.spot * 50))
    ratio = (state.atm_iv_near / state.rv_annual) if state.atm_iv_near and state.rv_annual else None
    short_vol = strategy_id in {BULL_PUT, BEAR_CALL, IRON_CONDOR, IRON_BUTTERFLY, CALENDAR}
    if ratio is None:
        v = 50.0
    else:
        v = _clamp(50 + 100 * (ratio - 1)) if short_vol else _clamp(50 - 100 * (ratio - 1))
    o = _oi_score(strategy_id, legs, ctx)
    credit_like = net_debit < 0
    if max_profit is None:
        r = 50.0
    elif credit_like:
        r = _clamp(100 * float(max_profit / max_loss) / 0.5)
    else:
        r = _clamp(100 * float(max_profit / max_loss) / 1.5)
    c = (
        100.0
        if max_profit is None
        else _clamp(100 * (1 - float(costs / (max_profit * Decimal("0.25")))))
    )
    comps = {
        "P": 100 * pop,
        "L": sum(liquidity) / len(liquidity),
        "V": v,
        "G": g,
        "O": o,
        "R": r,
        "C": c,
    }
    score = sum(WEIGHTS[k] * comps[k] for k in WEIGHTS)
    return _Build(
        Proposal(
            strategy_id=strategy_id,
            regime=REGIME_FOR[strategy_id],
            legs=tuple(legs),
            prices=tuple(prices),
            lot_size=lot,
            net_debit_per_lot=Decimal(str(net_debit)),
            max_loss_per_lot=max_loss,
            max_profit_per_lot=max_profit,
            costs_per_lot=costs,
            breakevens=breakevens,
            pop=pop,
            expected_value_per_lot=ev,
            stress_worst_per_lot=stress_report.worst_pnl,
            score=score,
            components=comps,
            exit_plan=exit_plan,
            rationale=rationale,
            data_timestamp=min(stamps),
            multi_expiry=multi_expiry,
        ),
        "",
    )


def _oi_score(
    strategy_id: str, legs: list[tuple[OptionAnalytics, Side]], ctx: MarketContext
) -> float:
    """Short strikes beyond the largest OI concentration on their side score higher.

    OI is context only (it can represent hedges either way); it never sets direction.
    """
    if not ctx.oi:
        return 50.0
    spot = ctx.state.spot
    scores = []
    for a, side in legs:
        if side is not Side.SELL:
            continue
        same_side = [
            x
            for x in ctx.near.analytics
            if x.contract.right is a.contract.right
            and (
                (x.contract.strike > Decimal(str(spot)))
                if a.contract.right is Right.CE
                else (x.contract.strike < Decimal(str(spot)))
            )
        ]
        if not same_side:
            continue
        wall = max(same_side, key=lambda x: ctx.oi.get(x.contract.instrument_token, 0))
        if a.contract.right is Right.CE:
            scores.append(100.0 if a.contract.strike >= wall.contract.strike else 55.0)
        else:
            scores.append(100.0 if a.contract.strike <= wall.contract.strike else 55.0)
    return sum(scores) / len(scores) if scores else 50.0


def _best(builds: list[_Build]) -> tuple[Proposal | None, str, int]:
    good = [b.proposal for b in builds if b.proposal is not None]
    rejected = len(builds) - len(good)
    if not good:
        reasons: dict[str, int] = {}
        for b in builds:
            key = b.reason.split(" Rs")[0] if "budget" in b.reason else b.reason
            reasons[key] = reasons.get(key, 0) + 1
        if not builds:
            return None, "no listed strikes inside the delta bands", 0
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        sample = next((b.reason for b in builds if "budget" in b.reason), "")
        detail = "; ".join(f"{k} x{n}" for k, n in top)
        return (
            None,
            f"all {rejected} structures rejected ({detail}){' e.g. ' + sample if sample else ''}",
            rejected,
        )
    return max(good, key=lambda p: p.score), "", rejected


# ------------------------------------------------------ structure builders
def build_bull_call(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, relax: bool = False
):
    calls = _by_right(ctx.near, Right.CE)
    longs = _in_band(calls, 0.55, 0.65, cfg.delta_tolerance)
    shorts = _in_band(calls, 0.25, 0.35, cfg.delta_tolerance)
    if relax:  # narrower spread: short call anywhere between the long leg and the band
        shorts = _delta_range(calls, 0.25 - cfg.delta_tolerance, 0.55)
    s = ctx.state
    resistances = [
        x for x in (s.prev_day_high, s.prev_week_high, _resistance(s)) if x and x > s.spot
    ]
    plan = ExitPlan(
        0.5,
        "max_profit",
        0.5,
        None,
        invalidate_below=_support(s),
        invalidate_above=None,
        alignment="bullish",
    )

    def rule_for(short_strike: float):
        def rule(max_loss, max_profit, debit):
            if max_profit is None or max_profit / max_loss < Decimal(str(cfg.min_debit_rr)):
                return f"reward/risk below {cfg.min_debit_rr}"
            blocking = [
                r for r in resistances if s.spot < r < s.spot + 0.5 * (short_strike - s.spot)
            ]
            if blocking:
                return f"major resistance {min(blocking):.0f} inside expected reward distance"
            return ""

        return rule

    builds = [
        _price_structure(
            BULL_CALL,
            [(lo, Side.BUY), (sh, Side.SELL)],
            ctx,
            cfg,
            risk_budget=budget,
            exit_plan=plan,
            rationale="Bullish HH-HL breakout; defined-risk call debit vertical",
            reject_rule=rule_for(float(sh.contract.strike)),
        )
        for lo, sh in product(longs, shorts)
        if sh.contract.strike > lo.contract.strike
    ]
    return _best(builds)


def build_bear_put(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, relax: bool = False
):
    puts = _by_right(ctx.near, Right.PE)
    longs = _in_band(puts, 0.55, 0.65, cfg.delta_tolerance)
    shorts = _in_band(puts, 0.25, 0.35, cfg.delta_tolerance)
    if relax:
        shorts = _delta_range(puts, 0.25 - cfg.delta_tolerance, 0.55)
    s = ctx.state
    supports = [x for x in (s.prev_day_low, s.prev_week_low, _support(s)) if x and x < s.spot]
    plan = ExitPlan(
        0.5,
        "max_profit",
        0.5,
        None,
        invalidate_below=None,
        invalidate_above=_resistance(s),
        alignment="bearish",
    )

    def rule_for(short_strike: float):
        def rule(max_loss, max_profit, debit):
            if max_profit is None or max_profit / max_loss < Decimal(str(cfg.min_debit_rr)):
                return f"reward/risk below {cfg.min_debit_rr}"
            blocking = [x for x in supports if s.spot - 0.5 * (s.spot - short_strike) < x < s.spot]
            if blocking:
                return f"major support {max(blocking):.0f} inside expected reward distance"
            return ""

        return rule

    builds = [
        _price_structure(
            BEAR_PUT,
            [(lo, Side.BUY), (sh, Side.SELL)],
            ctx,
            cfg,
            risk_budget=budget,
            exit_plan=plan,
            rationale="Bearish LL-LH breakdown; defined-risk put debit vertical",
            reject_rule=rule_for(float(sh.contract.strike)),
        )
        for lo, sh in product(longs, shorts)
        if sh.contract.strike < lo.contract.strike
    ]
    return _best(builds)


def _credit_rule(cfg: StrategyConfig, width: Decimal, lot: int, min_ratio: float):
    def rule(max_loss, max_profit, debit):
        credit = -debit
        if credit <= 0:
            return "structure is not a net credit"
        if credit / (width * lot) < Decimal(str(min_ratio)):
            return f"credit/width {float(credit / (width * lot)):.2f} < {min_ratio}"
        return ""

    return rule


def build_bull_put(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, relax: bool = False
):
    puts = _by_right(ctx.near, Right.PE)
    shorts = _in_band(puts, 0.20, 0.30, cfg.delta_tolerance)
    longs = _in_band(puts, 0.05, 0.15, cfg.delta_tolerance)
    if relax:  # narrower wing: protective put closer to the short strike
        longs = _delta_range(puts, 0.02, 0.30)
    s = ctx.state
    support = _support(s)
    plan = ExitPlan(
        0.5,
        "max_profit",
        1.0,
        0.45,
        invalidate_below=support,
        invalidate_above=None,
        alignment="bullish",
    )
    builds = []
    for sh, lo in product(shorts, longs):
        if lo.contract.strike >= sh.contract.strike:
            continue
        if support is not None and float(sh.contract.strike) >= support:
            builds.append(_Build(None, "short put not below confirmed support"))
            continue
        width = sh.contract.strike - lo.contract.strike
        builds.append(
            _price_structure(
                BULL_PUT,
                [(lo, Side.BUY), (sh, Side.SELL)],
                ctx,
                cfg,
                risk_budget=budget,
                exit_plan=plan,
                rationale="Higher-low support defence; protected put credit spread",
                reject_rule=_credit_rule(cfg, width, sh.contract.lot_size, cfg.min_credit_to_width),
            )
        )
    return _best(builds)


def build_bear_call(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, relax: bool = False
):
    calls = _by_right(ctx.near, Right.CE)
    shorts = _in_band(calls, 0.20, 0.30, cfg.delta_tolerance)
    longs = _in_band(calls, 0.05, 0.15, cfg.delta_tolerance)
    if relax:
        longs = _delta_range(calls, 0.02, 0.30)
    s = ctx.state
    resistance = _resistance(s)
    plan = ExitPlan(
        0.5,
        "max_profit",
        1.0,
        0.45,
        invalidate_below=None,
        invalidate_above=resistance,
        alignment="bearish",
    )
    builds = []
    for sh, lo in product(shorts, longs):
        if lo.contract.strike <= sh.contract.strike:
            continue
        if resistance is not None and float(sh.contract.strike) <= resistance:
            builds.append(_Build(None, "short call not above confirmed resistance"))
            continue
        width = lo.contract.strike - sh.contract.strike
        builds.append(
            _price_structure(
                BEAR_CALL,
                [(lo, Side.BUY), (sh, Side.SELL)],
                ctx,
                cfg,
                risk_budget=budget,
                exit_plan=plan,
                rationale="Lower-high resistance rejection; protected call credit spread",
                reject_rule=_credit_rule(cfg, width, sh.contract.lot_size, cfg.min_credit_to_width),
            )
        )
    return _best(builds)


def build_iron_condor(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, relax: bool = False
):
    s = ctx.state
    em = s.expected_move()
    if em is None:
        return None, "expected move unavailable (no ATM IV)", 0
    puts, calls = _by_right(ctx.near, Right.PE), _by_right(ctx.near, Right.CE)
    # Requirement order: short strikes outside the projected range first, then delta band.
    put_floor = min(s.range_low, s.spot - em)
    call_floor = max(s.range_high, s.spot + em)
    sp = _in_band(
        [a for a in puts if float(a.contract.strike) < put_floor], 0.15, 0.20, cfg.delta_tolerance
    )
    sc = _in_band(
        [a for a in calls if float(a.contract.strike) > call_floor], 0.15, 0.20, cfg.delta_tolerance
    )
    lp = _in_band(puts, 0.05, 0.10, cfg.delta_tolerance)
    lc = _in_band(calls, 0.05, 0.10, cfg.delta_tolerance)
    if relax:
        lp = _delta_range(puts, 0.02, 0.20)
        lc = _delta_range(calls, 0.02, 0.20)
    if not sp or not sc:
        return None, f"no short strikes outside range and expected move (+/-{em:.0f})", 0
    plan = ExitPlan(
        0.5, "max_profit", 1.0, 0.35, None, None, range_low=s.range_low, range_high=s.range_high
    )
    builds = []
    for spt, lpt, sct, lct in product(sp, lp, sc, lc):
        if not (
            lpt.contract.strike < spt.contract.strike and lct.contract.strike > sct.contract.strike
        ):
            continue
        width = max(
            spt.contract.strike - lpt.contract.strike, lct.contract.strike - sct.contract.strike
        )
        builds.append(
            _price_structure(
                IRON_CONDOR,
                [(lpt, Side.BUY), (lct, Side.BUY), (spt, Side.SELL), (sct, Side.SELL)],
                ctx,
                cfg,
                risk_budget=budget,
                exit_plan=plan,
                rationale=(
                    f"Confirmed range {s.range_low:.0f}-{s.range_high:.0f}; "
                    f"shorts beyond EM {em:.0f}"
                ),
                reject_rule=_credit_rule(
                    cfg, width, spt.contract.lot_size, cfg.min_condor_credit_to_width
                ),
            )
        )
    return _best(builds)


def build_iron_butterfly(ctx: MarketContext, cfg: StrategyConfig, budget: Decimal):
    s = ctx.state
    puts, calls = _by_right(ctx.near, Right.PE), _by_right(ctx.near, Right.CE)
    if not puts or not calls:
        return None, "chain incomplete", 0
    strikes = sorted({a.contract.strike for a in calls} & {a.contract.strike for a in puts})
    if not strikes:
        return None, "no paired ATM strike", 0
    atm = min(strikes, key=lambda k: abs(float(k) - s.spot))
    sc = next(a for a in calls if a.contract.strike == atm)
    spt = next(a for a in puts if a.contract.strike == atm)
    plan = ExitPlan(
        0.4, "max_profit", 1.0, None, None, None, range_low=s.range_low, range_high=s.range_high
    )
    builds = []
    for lct in (a for a in calls if a.contract.strike > atm):
        for lpt in (a for a in puts if a.contract.strike < atm):
            if lct.contract.strike - atm != atm - lpt.contract.strike:
                continue  # symmetric wings
            width = lct.contract.strike - atm
            builds.append(
                _price_structure(
                    IRON_BUTTERFLY,
                    [(lpt, Side.BUY), (lct, Side.BUY), (spt, Side.SELL), (sc, Side.SELL)],
                    ctx,
                    cfg,
                    risk_budget=budget * cfg.ib_risk_fraction,
                    exit_plan=plan,
                    rationale=f"Tight range pinned near {atm}; ATM iron butterfly",
                    reject_rule=_credit_rule(
                        cfg, width, sc.contract.lot_size, cfg.min_ib_credit_to_width
                    ),
                )
            )
    return _best(builds)


def build_calendar(ctx: MarketContext, cfg: StrategyConfig, budget: Decimal):
    if ctx.far is None:
        return None, "no back-month expiry available", 0
    s = ctx.state
    right = Right.CE if s.bias != "bearish" else Right.PE
    near = _by_right(ctx.near, right)
    far = {a.contract.strike: a for a in _by_right(ctx.far, right)}
    if not near:
        return None, "near chain incomplete", 0
    em = s.expected_move() or s.atr * 4
    plan = ExitPlan(
        0.15, "debit", 0.30, None, invalidate_below=s.spot - em, invalidate_above=s.spot + em
    )
    builds = []
    shorts = _in_band(near, 0.40, 0.55, cfg.delta_tolerance)
    for short in shorts:
        # Calendar: same strike. Diagonal (mild bias): long further-dated leg one step ITM.
        long_strikes = [short.contract.strike]
        if s.bias in {"bullish", "bearish"}:
            step = sorted(far)
            idx = step.index(short.contract.strike) if short.contract.strike in far else -1
            if idx > 0 and right is Right.CE:
                long_strikes.append(step[idx - 1])
            if 0 <= idx < len(step) - 1 and right is Right.PE:
                long_strikes.append(step[idx + 1])
        for k in long_strikes:
            longleg = far.get(k)
            if longleg is None:
                continue
            # Geometry that bounds loss by the debit: long strike at-or-better than short.
            if (right is Right.CE and k > short.contract.strike) or (
                right is Right.PE and k < short.contract.strike
            ):
                continue

            def rule(max_loss, max_profit, debit, _cost=None):
                if max_profit is None or max_profit <= 0:
                    return "no positive modelled payoff by session end"
                return ""

            builds.append(
                _price_structure(
                    CALENDAR,
                    [(longleg, Side.BUY), (short, Side.SELL)],
                    ctx,
                    cfg,
                    risk_budget=budget,
                    exit_plan=plan,
                    multi_expiry=True,
                    rationale=(
                        "Front IV rich vs back month; "
                        + ("calendar" if k == short.contract.strike else "diagonal")
                    ),
                    reject_rule=rule,
                )
            )
    return _best(builds)


def build_long_iron_butterfly(ctx: MarketContext, cfg: StrategyConfig, budget: Decimal):
    s = ctx.state
    puts, calls = _by_right(ctx.near, Right.PE), _by_right(ctx.near, Right.CE)
    strikes = sorted({a.contract.strike for a in calls} & {a.contract.strike for a in puts})
    if not strikes:
        return None, "no paired ATM strike", 0
    atm = min(strikes, key=lambda k: abs(float(k) - s.spot))
    lc = next(a for a in calls if a.contract.strike == atm)
    lp = next(a for a in puts if a.contract.strike == atm)
    em = s.expected_move()
    plan = ExitPlan(0.4, "debit", 0.35, None, None, None, time_stop_bars=6)
    builds = []
    for sct in (a for a in calls if a.contract.strike > atm):
        for spt in (a for a in puts if a.contract.strike < atm):
            if sct.contract.strike - atm != atm - spt.contract.strike:
                continue

            def rule(max_loss, max_profit, debit, _w=sct.contract.strike - atm):
                if debit <= 0:
                    return "long iron butterfly must be a debit"
                if max_profit is None or max_profit / max_loss < Decimal(str(cfg.min_long_ib_rr)):
                    return f"max profit/debit below {cfg.min_long_ib_rr}"
                per_unit = debit / lc.contract.lot_size
                if em is not None and Decimal(str(em)) < per_unit:
                    return f"expected move {em:.0f} < breakeven distance {per_unit:.0f}"
                return ""

            builds.append(
                _price_structure(
                    LONG_IRON_BUTTERFLY,
                    [(lc, Side.BUY), (lp, Side.BUY), (sct, Side.SELL), (spt, Side.SELL)],
                    ctx,
                    cfg,
                    risk_budget=budget,
                    exit_plan=plan,
                    rationale="Compression breakout; defined-debit long iron butterfly",
                    reject_rule=rule,
                )
            )
    return _best(builds)


BUILDERS = {
    BULL_CALL: build_bull_call,
    BEAR_PUT: build_bear_put,
    BULL_PUT: build_bull_put,
    BEAR_CALL: build_bear_call,
    IRON_CONDOR: build_iron_condor,
    IRON_BUTTERFLY: build_iron_butterfly,
    CALENDAR: build_calendar,
    LONG_IRON_BUTTERFLY: build_long_iron_butterfly,
}


CAPITAL_FIT = frozenset({BULL_CALL, BEAR_PUT, BULL_PUT, BEAR_CALL, IRON_CONDOR})


def regime_check(strategy_id: str, state: MarketState) -> Check:
    """The classified regime must support the family's thesis (spec 5.1 / 8.1).

    Uncertain regimes block everything except the defined-debit long-volatility
    structure, which the requirement allows when direction is unconfirmed.
    """
    regime, bias = state.regime, state.bias
    direction = DIRECTION[strategy_id]
    if regime is Regime.UNKNOWN:
        ok = False
    elif direction == "volatility":
        ok = regime not in {Regime.BULL_TREND, Regime.BEAR_TREND}
    elif direction == "neutral":
        ok = regime in {Regime.RANGE, Regime.VOLATILITY_COMPRESSION}
    elif direction == "bullish":
        ok = (
            regime is Regime.BULL_TREND
            or (regime is Regime.VOLATILITY_EXPANSION and bias == "bullish")
            or (strategy_id == BULL_PUT and regime is Regime.RANGE)
        )
    else:
        ok = (
            regime is Regime.BEAR_TREND
            or (regime is Regime.VOLATILITY_EXPANSION and bias == "bearish")
            or (strategy_id == BEAR_CALL and regime is Regime.RANGE)
        )
    return _c("regime supports thesis", ok, f"{regime.value} ({bias})")


def evaluate_all(
    ctx: MarketContext,
    *,
    risk_budget: Decimal,
    cfg: StrategyConfig = StrategyConfig(),
    enabled: frozenset[str] | None = None,
) -> list[Evaluation]:
    """Evaluate every family in priority order; structures are built only when setup passes."""
    results: list[Evaluation] = []
    for priority, sid in enumerate(PRIORITY, start=1):
        if enabled is not None and sid not in enabled:
            results.append(Evaluation(sid, priority, (), None, "disabled by configuration"))
            continue
        checks = (regime_check(sid, ctx.state), *SETUPS[sid](ctx.state, ctx.now))
        if not all(c.passed for c in checks):
            results.append(Evaluation(sid, priority, checks, None, ""))
            continue
        try:
            proposal, why, rejected = BUILDERS[sid](ctx, cfg, risk_budget)
            if proposal is None and "budget" in why and sid in CAPITAL_FIT:
                # Dynamic capital: keep the probability-defining leg inside its delta band
                # and narrow the protective/profit-cap leg until the worst case fits.
                fitted, fit_why, fit_rejected = BUILDERS[sid](ctx, cfg, risk_budget, relax=True)
                rejected += fit_rejected
                if fitted is not None:
                    proposal = replace(
                        fitted, rationale=fitted.rationale + " (width narrowed to fit capital)"
                    )
                else:
                    why = f"{why}; capital-fit pass: {fit_why}"
        except Exception as exc:  # a builder defect must not crash the cycle
            proposal, why, rejected = None, f"builder error: {type(exc).__name__}: {exc}", 0
        if proposal is not None and proposal.score < cfg.min_score:
            why, proposal = f"best score {proposal.score:.1f} < minimum {cfg.min_score}", None
        results.append(Evaluation(sid, priority, checks, proposal, why, rejected))
    return results


__all__ = [
    "BULL_CALL",
    "BEAR_PUT",
    "BULL_PUT",
    "BEAR_CALL",
    "IRON_CONDOR",
    "IRON_BUTTERFLY",
    "CALENDAR",
    "LONG_IRON_BUTTERFLY",
    "PRIORITY",
    "DIRECTION",
    "Check",
    "Evaluation",
    "ExitPlan",
    "MarketContext",
    "Proposal",
    "StrategyConfig",
    "evaluate_all",
    "STRATEGY_VERSION",
    "Contract",
]
