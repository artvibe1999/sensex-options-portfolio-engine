"""Deterministic, reason-coded exit decisions for open engine positions.

Priority (requirement 11.1): system/portfolio stops are applied by the engine;
this module covers strategy invalidation / regime flip, session cutoff, profit
target, stop loss and time stop. Marks are executable liquidation prices
(long legs at bid, short legs at ask), never midpoints.

Stop semantics: for net-credit structures ``stop_loss_pct`` is a multiple of
the credit received (1.0 = exit when the spread costs twice the credit), capped
by the defined max loss; for debit structures it is a fraction of max loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from .book import BookPosition
from .domain import Regime
from .indicators import IST
from .market_state import MarketState
from .strategies import DIRECTION

SQUARE_OFF = time(15, 10)


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    category: str  # invalidation | cutoff | target | stop | time


def evaluate_exit(
    position: BookPosition,
    *,
    pnl: float,
    short_deltas: dict[int, float],
    state: MarketState | None,
    new_bar: bool,
    now: datetime,
    square_off: time = SQUARE_OFF,
) -> ExitDecision | None:
    plan = position.exit_plan
    direction = DIRECTION.get(position.strategy_id, "neutral")

    # 3. Strategy invalidation / regime flip (evaluated on completed bars only).
    if state is not None and new_bar:
        below, above = plan.get("invalidate_below"), plan.get("invalidate_above")
        if below is not None and state.close < below:
            return ExitDecision(
                f"close {state.close:.0f} below thesis support {below:.0f}", "invalidation"
            )
        if above is not None and state.close > above:
            return ExitDecision(
                f"close {state.close:.0f} above thesis resistance {above:.0f}", "invalidation"
            )
        lo, hi = plan.get("range_low"), plan.get("range_high")
        if lo is not None and hi is not None and not lo <= state.close <= hi:
            return ExitDecision(
                f"close {state.close:.0f} broke range {lo:.0f}-{hi:.0f}", "invalidation"
            )
        alignment = plan.get("alignment")
        if (
            alignment == "bullish"
            and state.ema9 < state.ema21
            and (state.below_vwap or not state.vwap_available)
        ):
            return ExitDecision("VWAP/EMA alignment reversed bearish", "invalidation")
        if (
            alignment == "bearish"
            and state.ema9 > state.ema21
            and (state.above_vwap or not state.vwap_available)
        ):
            return ExitDecision("VWAP/EMA alignment reversed bullish", "invalidation")
        if direction == "bullish" and state.regime is Regime.BEAR_TREND:
            return ExitDecision("regime flipped to bear trend", "invalidation")
        if direction == "bearish" and state.regime is Regime.BULL_TREND:
            return ExitDecision("regime flipped to bull trend", "invalidation")
        if direction == "neutral" and state.regime in {
            Regime.BULL_TREND,
            Regime.BEAR_TREND,
            Regime.VOLATILITY_EXPANSION,
        }:
            return ExitDecision(f"range thesis broken: regime {state.regime.value}", "invalidation")
    limit = plan.get("short_delta_limit")
    if limit is not None:
        for token, delta in short_deltas.items():
            if abs(delta) > limit:
                return ExitDecision(
                    f"short leg {token} |delta| {abs(delta):.2f} > {limit}", "invalidation"
                )

    # 4. Session cutoff (intraday-only policy).
    if now.astimezone(IST).time() >= square_off:
        return ExitDecision(f"intraday square-off at {square_off:%H:%M}", "cutoff")

    # 5. Profit target.
    target_pct = float(plan["profit_target_pct"])
    if plan.get("profit_basis") == "debit":
        basis = float(position.entry_net)
    else:
        basis = float(position.max_profit) if position.max_profit is not None else 0.0
    if basis > 0 and pnl >= target_pct * basis:
        return ExitDecision(f"profit target {target_pct:.0%} reached (Rs{pnl:,.0f})", "target")

    # Stop loss.
    stop_pct = float(plan["stop_loss_pct"])
    max_loss = float(position.max_loss)
    if position.is_credit:
        threshold = min(stop_pct * -float(position.entry_net), max_loss)
    else:
        threshold = stop_pct * max_loss
    if threshold > 0 and pnl <= -threshold:
        return ExitDecision(f"stop loss Rs{threshold:,.0f} hit (Rs{pnl:,.0f})", "stop")

    # 6. Time stop (long-volatility move did not develop).
    bars = plan.get("time_stop_bars")
    held = int(position.meta.get("bars_held", 0))
    if bars is not None and held >= bars and pnl <= 0:
        return ExitDecision(f"expected move did not develop within {bars} bars", "time")
    return None
