"""Human-readable Telegram reports for the strategy engine.

Plain text (no parse mode), one fact per line, short labelled sections, so
replies stay readable on a phone. Values that are unknown say so explicitly;
nothing is fabricated.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .indicators import IST

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

STRATEGY_NAMES = {
    "SENSEX_BULL_CALL_DEBIT": "Bull call spread",
    "SENSEX_BEAR_PUT_DEBIT": "Bear put spread",
    "SENSEX_BULL_PUT_CREDIT": "Bull put spread",
    "SENSEX_BEAR_CALL_CREDIT": "Bear call spread",
    "SENSEX_IRON_CONDOR": "Iron condor",
    "SENSEX_IRON_BUTTERFLY": "Iron butterfly",
    "SENSEX_CALENDAR_DIAGONAL": "Calendar / diagonal",
    "SENSEX_LONG_IRON_BUTTERFLY": "Long iron butterfly",
}
MAX_CHARS = 3900


def name(strategy_id: str) -> str:
    return STRATEGY_NAMES.get(strategy_id, strategy_id)


def rs(value: Any, decimals: int = 0) -> str:
    if value is None:
        return "n/a"
    number = Decimal(str(value))
    sign = "-" if number < 0 else ""
    return f"{sign}Rs {abs(number):,.{decimals}f}"


def pct(value: Any, decimals: int = 2) -> str:
    return f"{Decimal(str(value)) * 100:.{decimals}f}%"


def _limit(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 20] + "\n... (truncated)"


def policy_lines(policy: Any, indent: str = "  ") -> list[str]:
    return [
        f"{indent}Capital tier: {policy.tier.name}",
        f"{indent}Risk per trade: {pct(policy.trade_pct)} = {rs(policy.max_trade_risk)}"
        + ("" if policy.multiplier == 1 else f" (scaled x{policy.multiplier:.2f})"),
        f"{indent}Max open risk: {rs(policy.max_aggregate_risk)}",
        f"{indent}Daily loss stop: {pct(policy.limits.daily_loss_stop_pct, 1)}",
        f"{indent}Max positions: {policy.limits.max_concurrent_positions}",
        f"{indent}Drawdown from peak: {pct(policy.drawdown_pct)} ({policy.reason})",
    ]


def status(engine: Engine, ops: Any | None = None) -> str:
    lines = ["SENSEX Hedge Engine - status", ""]
    lines.append(f"Mode: {engine.mode.upper()}")
    lines.append(f"New entries: {'PAUSED' if engine.user_paused else 'active'}")
    if ops is not None:
        lines.append(f"Live gates: {'closed' if ops.paused else 'open'}")
        lines.append(f"Live request pending: {'yes' if ops.mode.live_requested_by else 'no'}")
        lines.append(
            f"Startup recovery: {'OK' if ops.startup_recovery_complete else 'NOT complete'}"
        )
    cycle = engine.last_cycle_at
    lines.append(
        f"Last cycle: {cycle.astimezone(IST):%d %b %H:%M:%S} IST"
        if cycle
        else "Last cycle: not run yet"
    )
    s = engine.last_state
    lines += ["", "Market"]
    if s is None:
        lines.append("  No market state yet (computed during 09:15-15:30 IST).")
    else:
        lines += [
            f"  SENSEX: {s.spot:,.1f} (bar {s.bar_time.astimezone(IST):%H:%M})",
            f"  Regime: {s.regime.value.replace('_', ' ')} ({s.bias})",
            f"  ADX: {s.adx:.1f} | RSI: {s.rsi:.1f}",
            f"  VWAP (futures): {'n/a' if s.vwap is None else f'{s.vwap:,.1f}'}",
            f"  ATM IV: {'n/a' if s.atm_iv_near is None else f'{s.atm_iv_near:.1%}'}"
            + (
                "" if s.expected_move() is None else f" | expected move +/-{s.expected_move():,.0f}"
            ),
        ]
    if engine.last_policy is not None:
        lines += ["", "Risk"] + policy_lines(engine.last_policy)
    if engine.last_arbitration:
        lines += ["", f"Last decision: {engine.last_arbitration}"]
    if engine.last_error:
        lines += ["", f"Warning: {engine.last_error}"]
    return _limit("\n".join(lines))


def strategies(engine: Engine) -> str:
    evaluations = engine.last_evaluations
    if not evaluations:
        return (
            "No strategy evaluation yet.\n"
            "Strategies are evaluated on every completed 5-minute bar, 09:30-14:30 IST."
        )
    s = engine.last_state
    header = "Strategy check"
    if s is not None:
        header += f" - bar {s.bar_time.astimezone(IST):%H:%M} IST, SENSEX {s.spot:,.0f}"
    lines = [header, ""]
    for ev in evaluations:
        title = f"{ev.priority}. {name(ev.strategy_id)}"
        if ev.eligible:
            p = ev.proposal
            lines.append(f"{title}: ELIGIBLE")
            lines.append(
                f"   Score {p.score:.1f} | POP {p.pop:.0%} | risk/lot {rs(p.risk_per_lot)}"
            )
            for (a, side), price in zip(p.legs, p.prices, strict=True):
                lines.append(f"   {side.value} {a.contract.tradingsymbol} @ {price}")
        else:
            lines.append(f"{title}: no trade")
            failed = [c for c in ev.checks if not c.passed]
            if ev.rejection:
                lines.append(f"   Reason: {ev.rejection}")
            elif failed:
                for c in failed[:3]:
                    lines.append(f"   - {c.name}" + (f": {c.detail}" if c.detail else ""))
                if len(failed) > 3:
                    lines.append(f"   - ... and {len(failed) - 3} more rule(s)")
        lines.append("")
    if engine.last_arbitration:
        lines.append(f"Decision: {engine.last_arbitration}")
    return _limit("\n".join(lines).rstrip())


def positions(engine: Engine) -> str:
    items = engine.book.open_positions()
    if not items:
        return "No open positions."
    lines = [f"Open positions ({len(items)})", ""]
    for p in items:
        lines += [
            f"{name(p.strategy_id)} [{p.mode.upper()}] x{p.lots} lot(s)",
            f"  Id: {p.id}",
            f"  Status: {p.status}",
            f"  Opened: {p.opened_at[11:16]} IST",
            "  P&L now: "
            + (rs(p.meta["last_pnl"], 2) if "last_pnl" in p.meta else "updates on next cycle"),
            f"  Max loss: {rs(p.max_loss)} | Max profit: "
            f"{'unbounded' if p.max_profit is None else rs(p.max_profit)}",
            "  Legs:",
        ]
        lines += [f"    {leg.side} {leg.tradingsymbol} @ {leg.entry_price}" for leg in p.legs]
        lines.append("")
    lines.append("Close one with: /close <id> confirm")
    return _limit("\n".join(lines))


def pnl(engine: Engine) -> str:
    today = engine.clock().astimezone(IST).date().isoformat()
    lines = ["Profit & loss (after charges)", ""]
    shown = False
    for mode in ("paper", "live"):
        closed = engine.book.closed_positions(mode=mode)
        open_count = len(engine.book.open_positions(mode))
        if not closed and not open_count:
            continue
        shown = True
        day = sum(
            (p.realized_pnl or 0 for p in closed if (p.closed_at or "")[:10] == today), Decimal(0)
        )
        total = sum((p.realized_pnl or 0 for p in closed), Decimal(0))
        wins = [p.realized_pnl for p in closed if (p.realized_pnl or 0) > 0]
        losses = [p.realized_pnl for p in closed if (p.realized_pnl or 0) < 0]
        gross_win, gross_loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
        lines += [
            f"{mode.upper()}",
            f"  Today: {rs(day, 2)}",
            f"  All time: {rs(total, 2)}",
            f"  Closed trades: {len(closed)} ({len(wins)} won, {len(losses)} lost)",
            f"  Win rate: {len(wins) / len(closed):.0%}" if closed else "  Win rate: n/a",
            f"  Profit factor: {gross_win / gross_loss:.2f}"
            if gross_loss
            else "  Profit factor: n/a",
            f"  Open positions: {open_count}",
            "",
        ]
    if not shown:
        return "No trades yet."
    return _limit("\n".join(lines).rstrip())


def capital(engine: Engine, *, risk_view: bool = False) -> str:
    now = engine.clock()
    lines = ["Risk and limits" if risk_view else "Capital", ""]
    for mode in ("paper", "live") if engine.live is not None else ("paper",):
        try:
            snap, policy = engine.portfolio(mode, now)
        except Exception as exc:
            lines += [f"{mode.upper()} account", f"  Unavailable: {exc}", ""]
            continue
        daily = snap.daily_realized_pnl + snap.daily_unrealized_pnl
        lines.append(f"{mode.upper()} account")
        if not risk_view:
            lines += [
                f"  Equity: {rs(snap.net_liquidation_equity, 2)}",
                f"  Start of day: {rs(snap.start_of_day_equity, 2)}",
                f"  Peak: {rs(snap.peak_equity, 2)}",
                f"  Today's P&L: {rs(daily, 2)}",
                f"  Margin available: {rs(snap.available_margin)}",
            ]
        lines += [
            f"  Open risk: {rs(snap.open_max_loss)} in {snap.open_positions} position(s)",
            f"  Entries today: {snap.entries_today} of {policy.limits.max_new_entries_per_day}",
        ]
        if risk_view:
            lines += [
                f"  Today's P&L: {rs(daily, 2)}",
                f"  Week drawdown: {pct(snap.weekly_drawdown_pct)} "
                f"(stop {pct(policy.limits.weekly_drawdown_stop_pct, 1)})",
                f"  Month drawdown: {pct(snap.monthly_drawdown_pct)} "
                f"(stop {pct(policy.limits.monthly_drawdown_stop_pct, 1)})",
            ]
        lines += policy_lines(policy) + [""]
    return _limit("\n".join(lines).rstrip())
