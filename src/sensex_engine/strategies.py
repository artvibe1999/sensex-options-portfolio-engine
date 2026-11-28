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

STRATEGY_VERSION = "catalogue-v2-winners"

# 2026-11-28 owner decision: keep only families with positive after-charge results in the
# archive replays (docs/engine.md "Strategy selection evidence"); bull call / bear put debit
# spreads lost money and iron condor / iron butterfly / calendar / long iron butterfly never
# qualified (no evidence), so they were removed.
LEG_IN_CALL = "SENSEX_LEGIN_LOCK_CALL"
LEG_IN_PUT = "SENSEX_LEGIN_LOCK_PUT"
BULL_PUT = "SENSEX_BULL_PUT_CREDIT"
BEAR_CALL = "SENSEX_BEAR_CALL_CREDIT"
LEG_IN = frozenset({LEG_IN_CALL, LEG_IN_PUT})

PRIORITY = (LEG_IN_CALL, LEG_IN_PUT, BULL_PUT, BEAR_CALL)
DIRECTION = {
    LEG_IN_CALL: "bullish",
    LEG_IN_PUT: "bearish",
    BULL_PUT: "bullish",
    BEAR_CALL: "bearish",
}
# Entry windows (IST). No entries in the opening 20 minutes; engine cutoff 14:30.
WINDOWS = {
    LEG_IN_CALL: (time(9, 35), time(13, 30)),
    LEG_IN_PUT: (time(9, 35), time(13, 30)),
    BULL_PUT: (time(9, 45), time(14, 30)),
    BEAR_CALL: (time(9, 45), time(14, 30)),
}
# Credit spreads only showed positive after-charge results from Rs 2L of capital
# (charges dominated below that).
MIN_EQUITY = {BULL_PUT: Decimal("200000"), BEAR_CALL: Decimal("200000")}
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
    kind: str = "standard"  # "standard" | "leg_in"
    hedge_trigger: float | None = None  # leg-in: lock when long premium is up this fraction
    lock_target_width: float | None = None  # leg-in: exit locked spread at this x width


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
    # Leg-in: risk used for sizing is the premium stop (+ charges), not the full premium.
    sizing_risk_per_lot: Decimal | None = None

    @property
    def risk_per_lot(self) -> Decimal:
        if self.sizing_risk_per_lot is not None:
            return self.sizing_risk_per_lot
        return self.max_loss_per_lot + self.costs_per_lot

    def describe_legs(self) -> str:
        return ", ".join(
            f"{side.value} {a.contract.tradingsymbol}@{price} (d={float(a.delta):+.2f})"
            for (a, side), price in zip(self.legs, self.prices, strict=True)
        )

    def to_risk_candidate(self, lots: int) -> StrategyCandidate:
        """Candidate for the RiskEngine budget check.

        Leg-in positions are stop-managed: the per-trade budget is checked against the
        stop-loss risk; the full premium is still bounded by the margin/capital checks.
        """
        candidate = self.to_candidate(lots)
        if self.sizing_risk_per_lot is None:
            return candidate
        return replace(
            candidate,
            max_loss=self.sizing_risk_per_lot * lots - self.costs_per_lot * lots
            if self.sizing_risk_per_lot > self.costs_per_lot
            else self.sizing_risk_per_lot * lots,
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
    delta_tolerance: float = 0.05  # widen bands once if no listed strike fits
    # Leg-in lock (tested variant "lock t40 s20"): premium stop, hedge trigger, target.
    leg_in_stop: float = 0.20
    leg_in_hedge_trigger: float = 0.40
    leg_in_target_width: float = 0.80
    leg_in_max_delta: float = 0.60
    leg_in_min_delta_small: float = 0.20  # below Rs 1L: cheaper OTM strikes fit the budget
    leg_in_min_delta: float = 0.30
    # Evidence exists only at 1 lot: multi-lot sizing at Rs 5L-10L turned the same signals
    # negative in the engine replay (deeper first-half drawdown, then risk throttling).
    leg_in_max_lots: int = 1
    capital_utilization: Decimal = Decimal("1.0")  # owner: up to 100% of capital deployed
    rate: float = 0.065
    charges: ChargeSchedule = field(default_factory=ChargeSchedule)


@dataclass(frozen=True)
class MarketContext:
    state: MarketState
    near: OptionChainSnapshot  # nearest non-expiring expiry
    far: OptionChainSnapshot | None
    oi: dict[int, int]  # instrument_token -> open interest
    now: datetime
    equity: Decimal | None = None  # current verified equity (capital-dependent rules)


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


def _leg_in_signal(s: MarketState, now: datetime, bullish: bool) -> list[Check]:
    sid = LEG_IN_CALL if bullish else LEG_IN_PUT
    st = s.structure
    if bullish:
        brk = st.bullish_bos or (
            s.opening_range_high is not None and s.close > s.opening_range_high >= s.prev_close
        )
        return [
            _window(sid, now),
            _c(
                "EMA9 > EMA21 > EMA50",
                s.ema9 > s.ema21 > s.ema50,
                f"{s.ema9:.1f}/{s.ema21:.1f}/{s.ema50:.1f}",
            ),
            _c(
                "ADX >= 20 and rising",
                s.adx >= 20 and s.adx_rising,
                f"ADX {s.adx:.1f} prev {s.adx_prev:.1f}",
            ),
            _c("+DI > -DI", s.plus_di > s.minus_di, f"{s.plus_di:.1f}/{s.minus_di:.1f}"),
            _c("RSI 55-75", 55 <= s.rsi <= 75, f"{s.rsi:.1f}"),
            _c("breakout (BOS or opening range)", brk, ""),
        ]
    brk = st.bearish_bos or (
        s.opening_range_low is not None and s.close < s.opening_range_low <= s.prev_close
    )
    return [
        _window(sid, now),
        _c(
            "EMA9 < EMA21 < EMA50",
            s.ema9 < s.ema21 < s.ema50,
            f"{s.ema9:.1f}/{s.ema21:.1f}/{s.ema50:.1f}",
        ),
        _c(
            "ADX >= 20 and rising",
            s.adx >= 20 and s.adx_rising,
            f"ADX {s.adx:.1f} prev {s.adx_prev:.1f}",
        ),
        _c("-DI > +DI", s.minus_di > s.plus_di, f"{s.minus_di:.1f}/{s.plus_di:.1f}"),
        _c("RSI 25-45", 25 <= s.rsi <= 45, f"{s.rsi:.1f}"),
        _c("breakdown (BOS or opening range)", brk, ""),
    ]


def setup_leg_in_call(s: MarketState, now: datetime) -> list[Check]:
    return _leg_in_signal(s, now, bullish=True)


def setup_leg_in_put(s: MarketState, now: datetime) -> list[Check]:
    return _leg_in_signal(s, now, bullish=False)


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
    short_vol = strategy_id in {BULL_PUT, BEAR_CALL}
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


def build_leg_in(
    ctx: MarketContext, cfg: StrategyConfig, budget: Decimal, *, bullish: bool
) -> tuple[Proposal | None, str, int]:
    """Leg 1 of the lock plan: buy one option, highest |delta| whose stop risk fits.

    Sizing risk = ask x lot x stop + round-trip charges. The whole premium must also fit
    the deployable capital (equity x capital_utilization).
    """
    sid = LEG_IN_CALL if bullish else LEG_IN_PUT
    right = Right.CE if bullish else Right.PE
    equity = ctx.equity or Decimal("0")
    floor = cfg.leg_in_min_delta_small if equity < Decimal("100000") else cfg.leg_in_min_delta
    deployable = equity * cfg.capital_utilization if equity else None
    options = [
        a for a in _by_right(ctx.near, right) if floor <= _abs_delta(a) <= cfg.leg_in_max_delta
    ]
    if not options:
        return None, f"no {right.value} strike with |delta| {floor:.2f}-{cfg.leg_in_max_delta}", 0
    rejected: dict[str, int] = {}
    fits: list[tuple[OptionAnalytics, Decimal, Decimal]] = []
    for a in options:
        ok, _, why = _leg_liquidity(a, Side.BUY, cfg)
        if not ok:
            rejected[why.split(" ")[-1] if "depth" in why else "spread"] = (
                rejected.get("liquidity", 0) + 1
            )
            continue
        lot = a.contract.lot_size
        premium = a.quote.ask * lot
        costs = round_trip_estimate([("BUY", a.quote.ask, lot)], cfg.charges)
        risk = premium * Decimal(str(cfg.leg_in_stop)) + costs
        if risk > budget:
            rejected["budget"] = rejected.get("budget", 0) + 1
            continue
        if deployable is not None and premium > deployable:
            rejected["capital"] = rejected.get("capital", 0) + 1
            continue
        fits.append((a, risk, costs))
    if not fits:
        cheapest = min(options, key=lambda a: a.quote.ask)
        need = cheapest.quote.ask * cheapest.contract.lot_size * Decimal(str(cfg.leg_in_stop))
        return (
            None,
            (
                f"no strike fits budget Rs{budget:,.0f} (cheapest stop risk "
                f"Rs{need:,.0f}; {rejected})"
            ),
            len(options),
        )
    a, risk, costs = max(fits, key=lambda item: _abs_delta(item[0]))
    lot = a.contract.lot_size
    price = a.quote.ask
    k = a.contract.strike
    state = ctx.state
    max_profit = None if bullish else (k - price) * lot
    years = years_between(ctx.now, expiry_close(a.contract.expiry))
    sign = 1 if bullish else -1

    def terminal(spot: float) -> float:
        intrinsic = max(sign * (spot - float(k)), 0.0)
        return (intrinsic - float(price)) * lot

    prob = expiry_probability(
        terminal, spot=state.spot, iv=float(a.implied_volatility), years=years, costs=float(costs)
    )
    _, liquidity, _ = _leg_liquidity(a, Side.BUY, cfg)
    plan = ExitPlan(
        profit_target_pct=cfg.leg_in_target_width,
        profit_basis="lock_width",
        stop_loss_pct=cfg.leg_in_stop,
        short_delta_limit=None,
        invalidate_below=None,
        invalidate_above=None,
        kind="leg_in",
        hedge_trigger=cfg.leg_in_hedge_trigger,
        lock_target_width=cfg.leg_in_target_width,
        alignment="bullish" if bullish else "bearish",
    )
    proposal = Proposal(
        strategy_id=sid,
        regime=Regime.BULL_TREND if bullish else Regime.BEAR_TREND,
        legs=((a, Side.BUY),),
        prices=(price,),
        lot_size=lot,
        net_debit_per_lot=price * lot,
        max_loss_per_lot=price * lot,
        max_profit_per_lot=max_profit,
        costs_per_lot=costs,
        breakevens=(k + price if bullish else k - price,),
        pop=prob.pop,
        expected_value_per_lot=prob.expected_value,
        stress_worst_per_lot=-float(price * lot),
        score=liquidity,  # informational only; leg-in is not score-gated
        components={"L": liquidity},
        exit_plan=plan,
        rationale=(
            f"Trend {'breakout' if bullish else 'breakdown'}: buy "
            f"{a.contract.tradingsymbol} (delta {float(a.delta):+.2f}); lock with a "
            f"short strike at +{cfg.leg_in_hedge_trigger:.0%}"
        ),
        data_timestamp=a.quote.timestamp,
        sizing_risk_per_lot=risk,
    )
    return proposal, "", sum(rejected.values())


def build_leg_in_call(ctx: MarketContext, cfg: StrategyConfig, budget: Decimal):
    return build_leg_in(ctx, cfg, budget, bullish=True)


def build_leg_in_put(ctx: MarketContext, cfg: StrategyConfig, budget: Decimal):
    return build_leg_in(ctx, cfg, budget, bullish=False)


SETUPS: dict[str, Callable[[MarketState, datetime], list[Check]]] = {
    LEG_IN_CALL: setup_leg_in_call,
    LEG_IN_PUT: setup_leg_in_put,
    BULL_PUT: setup_bull_put,
    BEAR_CALL: setup_bear_call,
}
REGIME_FOR = {
    LEG_IN_CALL: Regime.BULL_TREND,
    LEG_IN_PUT: Regime.BEAR_TREND,
    BULL_PUT: Regime.BULL_TREND,
    BEAR_CALL: Regime.BEAR_TREND,
}
BUILDERS = {
    LEG_IN_CALL: build_leg_in_call,
    LEG_IN_PUT: build_leg_in_put,
    BULL_PUT: build_bull_put,
    BEAR_CALL: build_bear_call,
}


CAPITAL_FIT = frozenset({BULL_PUT, BEAR_CALL})


def regime_check(strategy_id: str, state: MarketState) -> Check:
    """The classified regime must support the family's thesis (spec 5.1 / 8.1)."""
    regime, bias = state.regime, state.bias
    direction = DIRECTION[strategy_id]
    if regime is Regime.UNKNOWN:
        ok = False
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
        if sid in MIN_EQUITY:
            need = MIN_EQUITY[sid]
            checks = (
                _c(
                    f"capital >= Rs{need:,.0f}",
                    ctx.equity is not None and ctx.equity >= need,
                    "below this, backtests showed charges outweigh the edge",
                ),
                *checks,
            )
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
        if proposal is not None and sid not in LEG_IN and proposal.score < cfg.min_score:
            why, proposal = f"best score {proposal.score:.1f} < minimum {cfg.min_score}", None
        results.append(Evaluation(sid, priority, checks, proposal, why, rejected))
    return results


__all__ = [
    "LEG_IN_CALL",
    "LEG_IN_PUT",
    "LEG_IN",
    "BULL_PUT",
    "BEAR_CALL",
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
