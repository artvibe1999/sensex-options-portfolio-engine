"""Autonomous SENSEX portfolio engine loop (paper by default, live on confirmation).

Every cycle (default 30 s) during the regular session it:
1. refreshes the shared Kite token, contract master, quotes and 5-minute bars;
2. builds the market state, option chains, ATM IV and expected move;
3. marks and manages every open position (daily loss stop, invalidation,
   square-off, target, stop, time stop) at executable bid/ask;
4. on each newly completed 5-minute bar, evaluates all eight strategy
   families, arbitrates, sizes against current equity and the capital-tier
   policy, re-checks the existing RiskEngine, and executes.

Paper fills are simulated at bid/ask with the versioned charge model. Live
entries/exits go through the durable fill-confirmed live coordinator and are
only possible after Telegram /live + /confirm_live and every readiness gate.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import UUID, uuid4

from .arbitration import arbitrate
from .book import BookLeg, BookPosition, EngineBook, new_position_id
from .capital_policy import RiskPolicy, risk_policy
from .charges import ChargeSchedule, leg_charges
from .domain import OptionLeg, PortfolioSnapshot, Position, Side, TradingMode
from .exits import evaluate_exit
from .indicators import IST
from .market_feed import INDEX_TOKEN, FeedSnapshot, MarketFeed, atm_iv
from .market_state import MarketState, build_market_state, with_options
from .risk import RiskEngine
from .scenario import expiry_close, years_between
from .strategies import (
    LEG_IN,
    MarketContext,
    Proposal,
    StrategyConfig,
    evaluate_all,
)

logger = logging.getLogger(__name__)
SESSION_START, SESSION_END = time(9, 15), time(15, 30)
MAX_SPOT_AGE_SECONDS = 120
MARK_MAX_AGE_SECONDS = 180  # option quotes older than this cannot mark or close a position


@dataclass
class EngineConfig:
    cycle_seconds: int = 30
    square_off: time = time(15, 10)
    # Operator stop percentages (only set when given explicitly); they can only tighten.
    daily_stop_override: Decimal | None = None
    weekly_stop_override: Decimal | None = None
    monthly_stop_override: Decimal | None = None
    # Hard engine-level entry cutoff, independent of each strategy's own window.
    entry_cutoff: time = time(14, 30)
    max_entries_per_day: int = 3
    max_margin_utilization: Decimal = Decimal("1.0")  # owner: up to 100% of capital
    paper_start_capital: Decimal | None = None
    risk_scale: Decimal = Decimal("1")
    enabled_strategies: frozenset[str] | None = None
    freeze_quantity: int = 1000
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    charges: ChargeSchedule = field(default_factory=ChargeSchedule)


class Notifier:
    """Alert sink. The default logs only; Telegram is attached when configured."""

    def __init__(self, sender: Callable[[str], None] | None = None) -> None:
        self._sender = sender

    def attach(self, sender: Callable[[str], None]) -> None:
        self._sender = sender

    def send(self, text: str) -> None:
        logger.info("ALERT %s", text.replace("\n", " | "))
        if self._sender is not None:
            try:
                self._sender(text)
            except Exception:
                logger.exception("Alert delivery failed")


class LiveEntryError(RuntimeError):
    """A live entry/hedge that may have left broker exposure (halts live trading)."""


class LiveEntryRejected(RuntimeError):
    """Rejected before any order was sent (stale quote, gate, sizing): safe to skip."""


class LiveBridge:
    """Adapter between the engine book and the durable live coordinator."""

    def __init__(
        self,
        *,
        coordinator: Any,
        positions: Any,
        sizing: Any,
        adapter: Any,
        snapshot_provider: Callable[[], PortfolioSnapshot | None],
    ) -> None:
        self.coordinator = coordinator
        self.positions = positions
        self.sizing = sizing
        self.adapter = adapter
        self.snapshot_provider = snapshot_provider

    def open(
        self, proposal: Proposal, *, lots: int, policy: RiskPolicy, allocation_pct: Decimal
    ) -> tuple[str, int, dict[int, float]]:
        if proposal.multi_expiry:
            raise LiveEntryRejected("Multi-expiry live sizing is not supported; paper only")
        self.sizing.risk = RiskEngine(policy.limits)
        candidate = proposal.to_candidate(1)
        tokens = [leg.contract.instrument_token for leg in candidate.legs]
        quotes = self.adapter.quotes(tokens)
        if len(quotes) != len(tokens):
            raise LiveEntryRejected("Fresh quotes unavailable for every leg")
        candidate = replace(candidate, data_timestamp=min(q.timestamp for q in quotes.values()))
        extra = {}
        if proposal.sizing_risk_per_lot is not None:
            extra["stop_fraction"] = Decimal(str(proposal.exit_plan.stop_loss_pct))
        try:
            report = self.coordinator.execute_sized(
                candidate, max_lots=lots, allocation_pct=allocation_pct, **extra
            )
        except (ValueError, PermissionError) as exc:
            # Raised by sizing/approval before the coordinator submits anything.
            raise LiveEntryRejected(str(exc)) from exc
        snap = self.positions.snapshot(report.position_id)
        if not report.completed or snap is None:
            raise LiveEntryError(
                f"Live entry incomplete: {report.reason}; residual={report.residual_exposure}"
            )
        fills = {
            int(leg["instrument_token"]): float(leg["open_average_price"]) for leg in snap["legs"]
        }
        lot_size = proposal.lot_size
        live_lots = int(snap["legs"][0]["quantity"]) // lot_size
        return report.position_id, live_lots, fills

    def hedge(self, position: BookPosition, contract: Any, quantity: int) -> tuple[str, float]:
        """Sell the lock strike against the confirmed long (separate durable position)."""
        assert position.broker_position_id
        long_leg = position.legs[0]
        same_series = (
            contract.right.value == long_leg.right
            and contract.expiry.isoformat() == long_leg.expiry
        )
        further_otm = (
            contract.strike > Decimal(str(long_leg.strike))
            if long_leg.right == "CE"
            else contract.strike < Decimal(str(long_leg.strike))
        )
        if not same_series or not further_otm or quantity != long_leg.quantity:
            raise LiveEntryError("Hedge strike does not protect-match the long leg")
        hedge = Position(
            uuid4(),
            position.strategy_id,
            (OptionLeg(contract, Side.SELL, quantity // contract.lot_size),),
            TradingMode.LIVE,
            datetime.now(UTC),
        )
        report = self.coordinator.execute_protected_short(
            hedge, protecting_position_id=position.broker_position_id
        )
        snap = self.positions.snapshot(report.position_id)
        if not report.completed or snap is None:
            raise LiveEntryError(
                f"Hedge incomplete: {report.reason}; residual={report.residual_exposure}"
            )
        return report.position_id, float(snap["legs"][0]["open_average_price"])

    def _close_one(self, broker_position_id: str, contracts: dict[int, Any]) -> dict[int, float]:
        snap = self.positions.snapshot(broker_position_id)
        if snap is None:
            raise RuntimeError("Durable live position is unavailable")
        legs = []
        for leg in snap["legs"]:
            contract = contracts.get(int(leg["instrument_token"]))
            if contract is None:
                raise RuntimeError(f"Contract {leg['tradingsymbol']} missing from master")
            legs.append(
                OptionLeg(contract, Side(leg["side"]), int(leg["quantity"]) // contract.lot_size)
            )
        live_position = Position(
            UUID(broker_position_id),
            snap["strategy_id"],
            tuple(legs),
            TradingMode.LIVE,
            datetime.now(UTC),
        )
        from .execution import ExecutionPhase

        report = self.coordinator.execute(live_position, ExecutionPhase.CLOSE)
        final = self.positions.snapshot(broker_position_id)
        if not report.completed or final is None:
            raise RuntimeError(
                f"Live close incomplete: {report.reason}; residual={report.residual_exposure}"
            )
        return {
            int(leg["instrument_token"]): float(leg["close_average_price"]) for leg in final["legs"]
        }

    def close(self, position: BookPosition, contracts: dict[int, Any]) -> dict[int, float]:
        hedge_id = position.meta.get("hedge_broker_position_id")
        if hedge_id:  # leg-in lock: buy back the short first, then sell the long
            prices = self._close_one(str(hedge_id), contracts)
            assert position.broker_position_id
            prices.update(self._close_one(position.broker_position_id, contracts))
            return prices
        assert position.broker_position_id
        snap = self.positions.snapshot(position.broker_position_id)
        if snap is None:
            raise RuntimeError("Durable live position is unavailable")
        legs = []
        for leg in snap["legs"]:
            contract = contracts.get(int(leg["instrument_token"]))
            if contract is None:
                raise RuntimeError(f"Contract {leg['tradingsymbol']} missing from master")
            legs.append(
                OptionLeg(contract, Side(leg["side"]), int(leg["quantity"]) // contract.lot_size)
            )
        live_position = Position(
            UUID(position.broker_position_id),
            snap["strategy_id"],
            tuple(legs),
            TradingMode.LIVE,
            datetime.now(UTC),
        )
        from .execution import ExecutionPhase

        report = self.coordinator.execute(live_position, ExecutionPhase.CLOSE)
        final = self.positions.snapshot(position.broker_position_id)
        if not report.completed or final is None:
            raise RuntimeError(
                f"Live close incomplete: {report.reason}; residual={report.residual_exposure}"
            )
        return {
            int(leg["instrument_token"]): float(leg["close_average_price"]) for leg in final["legs"]
        }


class Engine:
    def __init__(
        self,
        *,
        feed: MarketFeed,
        book: EngineBook,
        notifier: Notifier,
        config: EngineConfig,
        token_refresh: Callable[[], Any] | None = None,
        operations: Any | None = None,
        live: LiveBridge | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.feed = feed
        self.book = book
        self.notifier = notifier
        self.config = config
        self.token_refresh = token_refresh
        self.operations = operations
        self.live = live
        self.clock = clock or (lambda: datetime.now(IST))
        self.user_paused = False
        self.last_state: MarketState | None = None
        self.last_evaluations: list = []
        self.last_arbitration: str = ""
        self.last_error: str = ""
        self.last_cycle_at: datetime | None = None
        self.last_snapshot: FeedSnapshot | None = None
        self.policies: dict[str, RiskPolicy] = {}
        self._last_bar: datetime | None = None
        self._summary_day: date | None = None
        self._stale_alerted = False
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ----------------------------------------------------------- mode/status
    @property
    def mode(self) -> str:
        if self.operations is not None and self.operations.mode.mode is TradingMode.LIVE:
            return "live"
        return "paper"

    def entries_allowed(self, mode: str) -> tuple[bool, str]:
        if self.user_paused:
            return False, "entries paused by operator"
        if mode == "live":
            ops = self.operations
            if ops is None or self.live is None:
                return False, "live execution is not configured"
            if ops.paused or ops.emergency_active or not ops.startup_recovery_complete:
                return False, "live gates closed (paused/emergency/recovery)"
        return True, ""

    # ----------------------------------------------------------------- loop
    def run_forever(self) -> None:
        logger.info("Engine loop started (cycle %ss)", self.config.cycle_seconds)
        if self.live is not None:
            try:  # crash-window guard: re-attach live fills the book never recorded
                if self.token_refresh is not None:
                    self.token_refresh()
                self.feed.refresh_contracts(self.clock(), force=True)
                self.adopt_orphan_live_positions()
            except Exception:
                logger.exception("Startup live-position adoption check failed")
                self.notifier.send(
                    "[LIVE] Startup check for unrecorded live positions could not run; "
                    "verify Kite positions manually before /live."
                )
        while not self._stop.is_set():
            started = _time.monotonic()
            try:
                self.cycle()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("Engine cycle failed")
            elapsed = _time.monotonic() - started
            self._stop.wait(max(1.0, self.config.cycle_seconds - elapsed))

    def stop(self) -> None:
        self._stop.set()

    def cycle(self) -> None:
        with self._lock:
            now = self.clock()
            local = now.astimezone(IST)
            self.last_cycle_at = now
            # Re-read the shared token file on EVERY cycle (also outside market hours), so
            # the broker monitor and Telegram commands pick up a new daily Kite login at
            # once instead of waiting for the next session.
            info = self.token_refresh() if self.token_refresh is not None else None
            if local.weekday() >= 5 or not SESSION_START <= local.time() <= SESSION_END:
                if local.time() > SESSION_END and self._summary_day != local.date():
                    self._daily_summary(local.date())
                return
            if info is not None:
                if not getattr(info, "token", ""):
                    self.last_error = (
                        "No Kite access token (shared token file missing and no env token)"
                    )
                    return
            self.feed.refresh_contracts(now)
            open_positions = self.book.open_positions()
            extra = {leg.instrument_token for p in open_positions for leg in p.legs}
            snap = self.feed.snapshot(now, extra_tokens=extra)
            self.last_snapshot = snap
            spot_age = (now - snap.spot_time).total_seconds()
            if spot_age > MAX_SPOT_AGE_SECONDS:
                self.last_error = (
                    f"SENSEX index quote is {spot_age:.0f}s old (market closed/holiday?)"
                )
                if open_positions and not self._stale_alerted:
                    self.notifier.send(
                        f"Market data STALE ({spot_age:.0f}s). New entries frozen; "
                        f"{len(open_positions)} position(s) held, will retry."
                    )
                    self._stale_alerted = True
                return
            self._stale_alerted = False
            state, new_bar = self._market_state(now, snap)
            near = self.feed.chain(snap, snap.near_expiry)
            far = self.feed.chain(snap, snap.far_expiry)
            if state is not None:
                iv_near, straddle = atm_iv(near)
                iv_far, _ = atm_iv(far)
                state = with_options(
                    state,
                    atm_iv_near=iv_near,
                    atm_iv_far=iv_far,
                    straddle_near=straddle,
                    days_to_near_expiry=years_between(now, expiry_close(snap.near_expiry)) * 365,
                )
                self.last_state = state
            deltas = {
                a.contract.instrument_token: float(a.delta)
                for chain in (near, far)
                if chain
                for a in chain.analytics
            }
            self._manage_positions(now, snap, state, new_bar, deltas)
            if new_bar and state is not None and near is not None:
                self._consider_entry(now, snap, state, near, far)
            self.last_error = ""

    # ------------------------------------------------------------ helpers
    def _market_state(self, now: datetime, snap: FeedSnapshot) -> tuple[MarketState | None, bool]:
        try:
            index_bars = self.feed.bars(INDEX_TOKEN, now)
            future_bars = self.feed.bars(snap.future_token, now) if snap.future_token else None
            state = build_market_state(
                index_bars, now=now, spot=float(snap.spot), future_bars=future_bars
            )
        except Exception as exc:
            self.last_error = f"market state unavailable: {exc}"[:300]
            return None, False
        new_bar = state.bar_time != self._last_bar
        if new_bar:
            self._last_bar = state.bar_time
        return state, new_bar

    def _marks(self, position: BookPosition, snap: FeedSnapshot) -> dict[int, float] | None:
        marks: dict[int, float] = {}
        for leg in position.legs:
            quote = snap.quotes.get(leg.instrument_token)
            if quote is None:
                return None
            # A momentarily empty book or a stale tick is "no price", never a zero mark:
            # zero would trigger stops / the daily loss stop on a data glitch.
            age = (snap.as_of - quote.timestamp).total_seconds()
            if age > MARK_MAX_AGE_SECONDS:
                return None
            if leg.side == "BUY":  # close by selling at bid
                if quote.bid <= 0:
                    return None
                marks[leg.instrument_token] = float(quote.bid)
            else:  # close by buying at ask
                if quote.ask <= 0:
                    return None
                marks[leg.instrument_token] = float(quote.ask)
        return marks

    def _exit_costs(self, position: BookPosition, prices: dict[int, float]) -> Decimal:
        total = Decimal("0")
        for leg in position.legs:
            side = "SELL" if leg.side == "BUY" else "BUY"
            total += leg_charges(
                side=side,
                price=Decimal(str(prices[leg.instrument_token])),
                quantity=leg.quantity,
                schedule=self.config.charges,
            ).total
        return total

    # ------------------------------------------------------------ equity
    def _kite_start_of_day_funds(self) -> Decimal:
        margins = self.feed.adapter.margins("equity")
        available = margins.get("available") or {}
        opening = Decimal(str(available.get("opening_balance", 0) or 0))
        payin = Decimal(str(available.get("intraday_payin", 0) or 0))
        value = opening + payin
        if value <= 0:
            value = Decimal(str(margins.get("net", 0) or 0))
        if value <= 0:
            raise RuntimeError("Kite funds are not positive")
        return value

    def start_of_day_equity(self, mode: str, today: date) -> Decimal:
        row = self.book.equity_day(mode, today)
        if row:
            return Decimal(row["start_equity"])
        if mode == "paper":
            base_raw = self.book.get_state("paper_base_capital")
            if base_raw is None:
                base = self.config.paper_start_capital or self._kite_start_of_day_funds()
                self.book.set_state("paper_base_capital", str(base))
                self.notifier.send(
                    f"Paper capital initialised at Rs{base:,.0f} "
                    "(compounds with realised paper P&L)."
                )
            else:
                base = Decimal(base_raw)
            # Start of day = base + P&L realised BEFORE today (today's is added live).
            start = (
                base
                + self.book.realized_total("paper")
                - self.book.realized_total("paper", day=today.isoformat())
            )
        else:
            # LIVE capital is always the Zerodha account's own funds (owner decision):
            # start-of-day balance (opening balance + intraday pay-in), which does not shrink
            # when this engine's positions use margin. Deployable margin is additionally
            # capped by the broker's current available funds (see portfolio()).
            start = self._kite_start_of_day_funds()
            return Decimal(
                self.book.start_day(mode, today, start, carry_peak=False)["start_equity"]
            )
        return Decimal(self.book.start_day(mode, today, start)["start_equity"])

    def _unrealized(self, positions: list[BookPosition], snap: FeedSnapshot | None) -> Decimal:
        total = Decimal("0")
        for p in positions:
            marks = self._marks(p, snap) if snap else None
            if marks is not None:
                total += Decimal(str(round(p.unrealized(marks) or 0.0, 2)))
            else:  # unknown mark: assume worst case, never optimistic
                total -= p.max_loss
        return total

    def portfolio(self, mode: str, now: datetime) -> tuple[PortfolioSnapshot, RiskPolicy]:
        today = now.astimezone(IST).date()
        sod = self.start_of_day_equity(mode, today)
        open_positions = self.book.open_positions(mode)
        realized_today = self.book.realized_total(mode, day=today.isoformat())
        unrealized = self._unrealized(open_positions, self.last_snapshot)
        equity = sod + realized_today + unrealized
        row = self.book.equity_day(mode, today) or {}
        peak = max(Decimal(row.get("peak_equity", sod)), equity)
        self.book.update_peak(mode, today, equity)
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        if mode == "live":
            # The Zerodha balance also moves with deposits, withdrawals, the other bot and
            # manual trades; only this engine's own losses count towards its drawdown.
            week_dd = self._own_drawdown(mode, week_start, unrealized) / sod
            month_dd = self._own_drawdown(mode, month_start, unrealized) / sod
        else:
            week_ref = self.book.period_reference(mode, week_start) or sod
            month_ref = self.book.period_reference(mode, month_start) or sod
            week_dd = max(Decimal("0"), (week_ref - equity) / week_ref)
            month_dd = max(Decimal("0"), (month_ref - equity) / month_ref)
        policy = self._policy(equity, peak)
        used_margin = sum(Decimal(str(p.meta.get("margin", p.max_loss))) for p in open_positions)
        family: dict[str, Decimal] = {}
        for p in open_positions:
            family[p.strategy_id] = (
                family.get(p.strategy_id, Decimal("0")) + p.max_loss + p.entry_costs
            )
        margin_room = max(equity * self.config.max_margin_utilization - used_margin, Decimal("0"))
        if mode == "live":
            # Never assume more margin than the broker reports as available right now.
            broker_net = Decimal(str(self.feed.adapter.margins("equity").get("net", 0) or 0))
            margin_room = min(margin_room, max(broker_net, Decimal("0")))
        snapshot = PortfolioSnapshot(
            net_liquidation_equity=equity,
            available_margin=margin_room,
            open_max_loss=sum((self._open_risk(p) for p in open_positions), Decimal("0")),
            daily_realized_pnl=realized_today,
            daily_unrealized_pnl=unrealized,
            weekly_drawdown_pct=week_dd,
            monthly_drawdown_pct=month_dd,
            open_positions=len(open_positions),
            entries_today=int(row.get("entries", 0)),
            peak_equity=peak,
            start_of_day_equity=sod,
            strategy_open_risk=tuple(sorted(family.items())),
        )
        self.policies[mode] = policy
        return snapshot, policy

    @property
    def last_policy(self) -> RiskPolicy | None:
        """Risk policy of the mode the engine is trading in now."""
        return self.policies.get(self.mode)

    def _own_drawdown(self, mode: str, since: date, unrealized: Decimal) -> Decimal:
        """Drawdown from this engine's OWN P&L since ``since`` (rupees, >= 0)."""
        closed = sorted(
            self.book.closed_positions(mode=mode, since_day=since.isoformat()),
            key=lambda p: p.closed_at or "",
        )
        cum = peak = Decimal("0")
        for p in closed:
            cum += p.realized_pnl or Decimal("0")
            peak = max(peak, cum)
        return max(Decimal("0"), peak - (cum + unrealized))

    @staticmethod
    def _open_risk(p: BookPosition) -> Decimal:
        """Risk an open position still carries (leg-in: stop risk, zero once locked)."""
        if p.exit_plan.get("kind") == "leg_in":
            if p.meta.get("stage") == "locked":
                return max(p.max_loss, Decimal("0"))
            return Decimal(str(p.meta.get("managed_risk", p.max_loss + p.entry_costs)))
        return p.max_loss + p.entry_costs

    def live_snapshot(self) -> PortfolioSnapshot | None:
        try:
            return self.portfolio("live", self.clock())[0]
        except Exception:
            logger.exception("Live portfolio snapshot unavailable")
            return None

    def live_limits(self):
        snap = self.live_snapshot()
        if snap is None:
            return None
        return self._policy(
            snap.net_liquidation_equity, snap.peak_equity or snap.net_liquidation_equity
        ).limits

    def _policy(self, equity: Decimal, peak: Decimal) -> RiskPolicy:
        return risk_policy(
            equity=equity,
            peak_equity=peak,
            max_new_entries_per_day=self.config.max_entries_per_day,
            max_margin_utilization_pct=self.config.max_margin_utilization,
            risk_scale=self.config.risk_scale,
            daily_stop_override=self.config.daily_stop_override,
            weekly_stop_override=self.config.weekly_stop_override,
            monthly_stop_override=self.config.monthly_stop_override,
        )

    # ------------------------------------------------------------ exits
    def _manage_positions(
        self,
        now: datetime,
        snap: FeedSnapshot,
        state: MarketState | None,
        new_bar: bool,
        deltas: dict[int, float],
    ) -> None:
        today = now.astimezone(IST).date()
        for mode in ("paper", "live"):
            positions = self.book.open_positions(mode)
            if not positions:
                continue
            portfolio, policy = self.portfolio(mode, now)
            daily = portfolio.daily_realized_pnl + portfolio.daily_unrealized_pnl
            limit = (
                portfolio.start_of_day_equity or portfolio.net_liquidation_equity
            ) * policy.limits.daily_loss_stop_pct
            # Unpriced legs are valued at max loss for sizing (never optimistic), but a
            # forced liquidation must not be triggered by that assumption alone.
            all_priced = all(self._marks(p, snap) is not None for p in positions)
            if daily <= -limit and not all_priced:
                self.last_error = "daily-stop check deferred: a leg has no executable quote"
            elif daily <= -limit:
                first = not (self.book.equity_day(mode, today) or {}).get("halted")
                if first:
                    self.book.set_halted(mode, today)
                for p in positions:
                    if p.status in {"OPEN"}:  # failed live closes (ERROR) need the operator
                        self._close(p, snap, f"daily loss stop Rs{limit:,.0f} reached", now)
                if first:
                    left = [p for p in self.book.open_positions(mode)]
                    self.notifier.send(
                        f"[{mode.upper()}] Daily loss stop hit (Rs{daily:,.0f}); entries halted "
                        "for today. "
                        + (
                            "All positions closed."
                            if not left
                            else f"{len(left)} position(s) could NOT be closed - check /positions."
                        )
                    )
                continue
            for p in positions:
                if p.status == "ERROR":
                    continue
                marks = self._marks(p, snap)
                if new_bar:
                    p.meta["bars_held"] = int(p.meta.get("bars_held", 0)) + 1
                    self.book.update_meta(p.id, p.meta)
                if marks is None:
                    if now.astimezone(IST).time() >= self.config.square_off:
                        self.notifier.send(
                            f"[{mode.upper()}] {p.strategy_id} {p.id}: cannot price "
                            "legs at square-off; retrying each cycle."
                        )
                    continue
                pnl = (p.unrealized(marks) or 0.0) - float(self._exit_costs(p, marks))
                p.meta["last_pnl"] = round(pnl, 2)
                p.meta["peak_pnl"] = max(float(p.meta.get("peak_pnl", pnl)), pnl)
                if p.exit_plan.get("kind") == "leg_in":
                    self.book.update_meta(p.id, p.meta)
                    self._manage_leg_in(p, snap, state, new_bar, now, marks)
                    continue
                shorts = {
                    leg.instrument_token: deltas[leg.instrument_token]
                    for leg in p.legs
                    if leg.side == "SELL" and leg.instrument_token in deltas
                }
                decision = evaluate_exit(
                    p,
                    pnl=pnl,
                    short_deltas=shorts,
                    state=state,
                    new_bar=new_bar,
                    now=now,
                    square_off=self.config.square_off,
                )
                self.book.update_meta(p.id, p.meta)
                if decision is not None:
                    self._close(p, snap, decision.reason, now)

    def _manage_leg_in(
        self,
        p: BookPosition,
        snap: FeedSnapshot,
        state: MarketState | None,
        new_bar: bool,
        now: datetime,
        marks: dict[int, float],
    ) -> None:
        """Leg-in lock: stop / EMA21 exit / lock hedge at +trigger / locked-spread target."""
        plan = p.exit_plan
        long_leg = p.legs[0]
        bid = marks[long_leg.instrument_token]
        entry = long_leg.entry_price
        cutoff = now.astimezone(IST).time() >= self.config.square_off
        if p.meta.get("stage") == "locked":
            short = p.legs[1]
            value = bid - marks[short.instrument_token]
            width = abs(short.strike - long_leg.strike)
            target = plan.get("lock_target_width")
            if target is not None and value >= float(target) * width:
                self._close(
                    p, snap, f"locked spread reached {plan['lock_target_width']:.0%} of width", now
                )
            elif cutoff:
                self._close(p, snap, f"intraday square-off at {self.config.square_off:%H:%M}", now)
            return
        if bid <= entry * (1 - float(plan["stop_loss_pct"])):
            self._close(p, snap, f"premium stop -{plan['stop_loss_pct']:.0%}", now)
            return
        if cutoff:
            self._close(p, snap, f"intraday square-off at {self.config.square_off:%H:%M}", now)
            return
        if plan.get("ema_exit", True) and new_bar and state is not None:
            bullish = plan.get("alignment") == "bullish"
            if (bullish and state.close < state.ema21) or (
                not bullish and state.close > state.ema21
            ):
                self._close(p, snap, "index closed back through EMA21", now)
                return
        if bid >= entry * (1 + float(plan["hedge_trigger"])):
            self._lock_hedge(p, snap, now, entry)

    def _lock_hedge(self, p: BookPosition, snap: FeedSnapshot, now: datetime, entry: float) -> None:
        """Sell the furthest-OTM same-series strike whose bid >= the long's entry price."""
        long_leg = p.legs[0]
        long_contract = self.feed.by_token.get(long_leg.instrument_token)
        if long_contract is None:
            return
        best = None
        for contract in self.feed.contracts:
            if (
                contract.expiry != long_contract.expiry
                or contract.right != long_contract.right
                or contract.instrument_token == long_contract.instrument_token
            ):
                continue
            further = (
                contract.strike > long_contract.strike
                if contract.right.value == "CE"
                else contract.strike < long_contract.strike
            )
            quote = snap.quotes.get(contract.instrument_token)
            if (
                not further
                or quote is None
                or float(quote.bid) < entry
                or quote.bid_quantity < long_leg.quantity
            ):
                continue
            distance = abs(contract.strike - long_contract.strike)
            if best is None or distance > best[0]:
                best = (distance, contract, quote)
        if best is None:
            return  # no strike locks yet; try again next cycle
        _, contract, quote = best
        price = float(quote.bid)
        meta = dict(p.meta)
        if p.mode == "live":
            if self.live is None or meta.get("hedge_attempted"):
                return  # exactly one live hedge attempt per position
            meta["hedge_attempted"] = now.isoformat()
            self.book.update_meta(p.id, meta)  # persisted before any order is sent
            p.meta = meta
            try:
                hedge_id, price = self.live.hedge(p, contract, long_leg.quantity)
            except Exception as exc:
                self.notifier.send(
                    f"[LIVE] HEDGE NOT PLACED for {p.id}: {exc}. Not retried; the long leg "
                    "stays under its stop and 15:10 square-off. Check Kite for any partial "
                    "short and use /emergency if needed."
                )
                return
            meta["hedge_broker_position_id"] = hedge_id
        leg = BookLeg(
            contract.instrument_token,
            contract.tradingsymbol,
            contract.right.value,
            float(contract.strike),
            contract.expiry.isoformat(),
            "SELL",
            long_leg.quantity,
            price,
            0.0,
        )
        costs = leg_charges(
            side="SELL",
            price=Decimal(str(price)),
            quantity=long_leg.quantity,
            schedule=self.config.charges,
        ).total
        meta.update(stage="locked", locked_at=now.isoformat())
        locked = self.book.add_hedge_leg(p.id, leg=leg, entry_costs=costs, meta=meta)
        from .reports import name, rs

        self.notifier.send(
            f"[{p.mode.upper()}] HEDGED {name(p.strategy_id)} - profit locked\n"
            f"Sold {contract.tradingsymbol} @ {price} against "
            f"{long_leg.tradingsymbol} (bought @ {long_leg.entry_price})\n"
            f"Worst case now: {rs(-locked.max_loss) if locked.max_loss else 'Rs 0'} "
            f"(before charges) | Max profit: {rs(locked.max_profit)}"
        )

    def _close(self, p: BookPosition, snap: FeedSnapshot, reason: str, now: datetime) -> None:
        try:
            if p.mode == "live":
                if self.live is None:
                    raise RuntimeError("live bridge unavailable")
                self.book.set_status(p.id, "CLOSING", reason)
                prices = self.live.close(p, self.feed.by_token)
            else:
                prices = self._marks(p, snap)
                if prices is None:
                    raise RuntimeError("no executable exit prices")
            costs = self._exit_costs(p, prices)
            pnl = self.book.close_position(
                p.id, exit_prices=prices, exit_costs=costs, reason=reason, closed_at=now
            )
        except Exception as exc:
            self.book.set_status(p.id, "ERROR" if p.mode == "live" else p.status, None)
            if not p.meta.get("exit_fail_alerted"):  # alert once per position, not per cycle
                p.meta["exit_fail_alerted"] = now.isoformat()
                self.book.update_meta(p.id, p.meta)
                self.notifier.send(
                    f"[{p.mode.upper()}] EXIT FAILED {p.strategy_id} {p.id}: {exc}. "
                    + ("Manual check required." if p.mode == "live" else "Retrying each cycle.")
                )
            return
        legs = "\n".join(
            f"  {leg.side} {leg.tradingsymbol}: {leg.entry_price} -> {prices[leg.instrument_token]}"
            for leg in p.legs
        )
        from .reports import name, rs

        self.notifier.send(
            f"[{p.mode.upper()}] EXIT {name(p.strategy_id)} x{p.lots}\n"
            f"Reason: {reason}\n"
            f"P&L: {rs(pnl, 2)} (charges {rs(p.entry_costs + costs, 2)})\n"
            f"Legs (entry -> exit):\n{legs}"
        )
        self.book.log_decision("exit", {"position": p.id, "reason": reason, "pnl": str(pnl)}, now)

    MIN_PAPER_CAPITAL = Decimal("10000")
    MAX_PAPER_CAPITAL = Decimal("100000000")

    def set_paper_capital(self, amount: Decimal, *, user_id: int | None = None) -> str:
        """Operator sets current paper equity to ``amount`` (paper only)."""
        from .reports import policy_lines, rs

        with self._lock:
            if not self.MIN_PAPER_CAPITAL <= amount <= self.MAX_PAPER_CAPITAL:
                return (
                    f"Paper capital must be between {rs(self.MIN_PAPER_CAPITAL)} and "
                    f"{rs(self.MAX_PAPER_CAPITAL)}."
                )
            open_paper = self.book.open_positions("paper")
            if open_paper:
                return (
                    f"Not changed: {len(open_paper)} paper position(s) are open. Close them "
                    "(/positions, /close <id> confirm) or wait for the 15:10 square-off."
                )
            now = self.clock()
            today = now.astimezone(IST).date()
            previous = self.book.get_state("paper_base_capital")
            # equity = base + all realised paper P&L, so solve for the base.
            base = amount - self.book.realized_total("paper")
            self.book.set_state("paper_base_capital", str(base))
            # portfolio() adds today's realised P&L on top of the day start, so start the
            # day at amount - realised_today: equity then reads exactly ``amount``.
            self.book.reset_equity_reference(
                "paper", today, amount - self.book.realized_total("paper", day=today.isoformat())
            )
            self.book.log_decision(
                "paper_capital_set",
                {"amount": str(amount), "previous_base": previous, "user": user_id},
                now,
            )
            policy = self._policy(amount, amount)
            self.policies["paper"] = policy
            self.notifier.send(f"[PAPER] Capital set to {rs(amount, 2)} by operator.")
            return "\n".join(
                [
                    f"Paper capital set to {rs(amount, 2)}",
                    "Drawdown and daily-loss references restart from this amount; "
                    "trade history is kept.",
                    "",
                    "New risk policy",
                    *policy_lines(policy),
                ]
            )

    def sync_live_book(self, reason: str) -> int:
        """Close live book rows whose durable broker positions are already flat."""
        if self.live is None:
            return 0
        synced = 0
        for p in self.book.open_positions("live"):
            ids = [i for i in (p.broker_position_id, p.meta.get("hedge_broker_position_id")) if i]
            snaps = [self.live.positions.snapshot(str(i)) for i in ids]
            if not snaps or any(s is None or s["status"] != "CLOSED" for s in snaps):
                continue
            prices = {}
            for s in snaps:
                for leg in s["legs"]:
                    if leg.get("close_average_price") is not None:
                        prices[int(leg["instrument_token"])] = float(leg["close_average_price"])
            if any(leg.instrument_token not in prices for leg in p.legs):
                continue
            self.book.close_position(
                p.id,
                exit_prices=prices,
                exit_costs=self._exit_costs(p, prices),
                reason=reason,
                closed_at=self.clock(),
            )
            synced += 1
        return synced

    def adopt_orphan_live_positions(self) -> list[str]:
        """Startup guard: durable LIVE positions the engine book does not know about.

        Covers a crash between a confirmed live fill and the book write. A single long
        leg-in leg is adopted (stop / hedge / 15:10 square-off resume); anything else is
        reported for manual handling.
        """
        if self.live is None:
            return []
        known = set()
        for p in self.book.open_positions("live") + self.book.closed_positions(mode="live"):
            known.update(
                i for i in (p.broker_position_id, p.meta.get("hedge_broker_position_id")) if i
            )
        reports = []
        for snap in self.live.positions.active_snapshots():
            pid = str(snap.get("position_id"))
            if snap.get("mode") != "live" or pid in known:
                continue
            legs = [leg for leg in snap["legs"] if int(leg.get("remaining_quantity", 0)) > 0]
            contract = self.feed.by_token.get(int(legs[0]["instrument_token"])) if legs else None
            if (
                snap["strategy_id"] in LEG_IN
                and len(legs) == 1
                and legs[0]["side"] == "BUY"
                and contract is not None
                and legs[0].get("open_average_price")
            ):
                price = Decimal(str(legs[0]["open_average_price"]))
                qty = int(legs[0]["remaining_quantity"])
                cfg = self.config.strategy
                plan = {
                    "kind": "leg_in",
                    "profit_target_pct": 0.0,
                    "profit_basis": "lock_width",
                    "stop_loss_pct": cfg.leg_in_stop,
                    "hedge_trigger": cfg.leg_in_hedge_trigger,
                    "lock_target_width": cfg.leg_in_target_width,
                    "ema_exit": cfg.leg_in_ema_exit,
                    "alignment": "bullish" if contract.right.value == "CE" else "bearish",
                }
                now = self.clock()
                self.book.open_position(
                    BookPosition(
                        id=new_position_id(),
                        mode="live",
                        strategy_id=snap["strategy_id"],
                        status="OPEN",
                        opened_at=now.isoformat(),
                        session_day=now.astimezone(IST).date().isoformat(),
                        lots=qty // contract.lot_size,
                        lot_size=contract.lot_size,
                        entry_net=price * qty,
                        max_loss=price * qty,
                        max_profit=None,
                        entry_costs=leg_charges(
                            side="BUY", price=price, quantity=qty, schedule=self.config.charges
                        ).total,
                        legs=[
                            BookLeg(
                                contract.instrument_token,
                                contract.tradingsymbol,
                                contract.right.value,
                                float(contract.strike),
                                contract.expiry.isoformat(),
                                "BUY",
                                qty,
                                float(price),
                                0.0,
                            )
                        ],
                        exit_plan=plan,
                        meta={
                            "stage": "single",
                            "adopted": True,
                            "managed_risk": str(price * qty * Decimal(str(cfg.leg_in_stop))),
                        },
                        broker_position_id=pid,
                    )
                )
                reports.append(f"adopted {pid} ({contract.tradingsymbol} x{qty})")
            else:
                reports.append(f"UNMANAGED durable live position {pid} ({snap['strategy_id']})")
        if reports:
            self.notifier.send(
                "[LIVE] Startup check found live positions not in the engine "
                "book:\n"
                + "\n".join(reports)
                + "\nAdopted legs are managed again; anything UNMANAGED needs "
                "manual handling (/emergency)."
            )
        return reports

    def close_all(self, mode: str, reason: str) -> int:
        """Operator-initiated controlled close (paper; live goes through emergency close-all)."""
        with self._lock:
            snap = self._fresh_snapshot()
            if snap is None:
                raise RuntimeError("market snapshot is missing or stale; cannot price closes")
            count = 0
            for p in self.book.open_positions(mode):
                self._close(p, snap, reason, self.clock())
                count += 1
            return count

    def _fresh_snapshot(self) -> FeedSnapshot | None:
        snap = self.last_snapshot
        if snap is None or (self.clock() - snap.as_of).total_seconds() > MAX_SPOT_AGE_SECONDS:
            return None
        return snap

    def close_one(self, position_id: str, *, user_id: int | None = None) -> str:
        """Operator-confirmed controlled close of one engine position."""
        with self._lock:
            position = self.book.get(position_id)
            if position is None or position.status == "CLOSED":
                return f"No open engine position {position_id}."
            snap = self._fresh_snapshot()
            if snap is None:
                return "Market snapshot is missing or stale; cannot price the close now."
            self._close(position, snap, f"operator /close by {user_id}", self.clock())
            after = self.book.get(position_id)
            if after is not None and after.status == "CLOSED":
                return f"Closed {position_id}: P&L Rs{after.realized_pnl:,.2f}."
            status = after.status if after else "?"
            return f"Close of {position_id} did not complete (status {status})."

    # ----------------------------------------------------------- entries
    def _consider_entry(
        self, now: datetime, snap: FeedSnapshot, state: MarketState, near, far
    ) -> None:
        mode = self.mode
        allowed, why = self.entries_allowed(mode)
        today = now.astimezone(IST).date()
        portfolio, policy = self.portfolio(mode, now)
        row = self.book.equity_day(mode, today) or {}
        ctx = MarketContext(
            state=state,
            near=near,
            far=far,
            oi=snap.oi(),
            now=now,
            equity=portfolio.net_liquidation_equity,
        )
        budget = policy.max_trade_risk
        evaluations = evaluate_all(
            ctx,
            risk_budget=budget,
            cfg=self.config.strategy,
            enabled=self.config.enabled_strategies,
        )
        self.last_evaluations = evaluations
        open_positions = self.book.open_positions(mode)
        # Leg-in: one entry per direction per day (no re-entry after an exit or a stop).
        today_iso = today.isoformat()
        traded_today = {
            p.strategy_id
            for p in self.book.closed_positions(mode=mode, since_day=today_iso)
            if p.strategy_id in LEG_IN and p.session_day == today_iso
        }
        result = arbitrate(
            evaluations,
            open_strategies=[p.strategy_id for p in open_positions],
            exclude=frozenset(traded_today),
            max_concurrent=policy.tier.max_concurrent_positions,
        )
        blocked = ""
        if now.astimezone(IST).time() >= self.config.entry_cutoff:
            blocked = f"entry cutoff {self.config.entry_cutoff:%H:%M} IST passed"
        elif not allowed:
            blocked = why
        elif row.get("halted"):
            blocked = "daily loss stop active for today"
        elif not policy.entries_allowed:
            blocked = f"drawdown policy: {policy.reason}"
        elif int(row.get("entries", 0)) >= self.config.max_entries_per_day:
            blocked = f"max {self.config.max_entries_per_day} entries per day reached"
        self.last_arbitration = blocked or result.reason
        self.book.log_decision(
            "evaluation",
            {
                "mode": mode,
                "bar": state.bar_time,
                "regime": state.regime.value,
                "spot": state.spot,
                "policy": policy.describe(),
                "arbitration": self.last_arbitration,
                "eligible": [e.strategy_id for e in evaluations if e.eligible],
            },
            now,
        )
        if blocked or result.selected is None:
            return
        proposal = result.selected
        lots, margin_per_lot, why = self._size(proposal, mode, portfolio, policy)
        if lots < 1:
            self.last_arbitration = f"{proposal.strategy_id} not sized: {why}"
            return
        self._open(proposal, lots, margin_per_lot, mode, policy, state, now)

    def _margin_per_lot(self, proposal: Proposal) -> tuple[Decimal, str]:
        legs = tuple(OptionLeg(a.contract, side, 1) for a, side in proposal.legs)
        prices = {
            a.contract.instrument_token: p
            for (a, _), p in zip(proposal.legs, proposal.prices, strict=True)
        }
        try:
            return self.feed.adapter.basket_margin(legs, prices), "broker basket margin"
        except Exception as exc:
            if proposal.strategy_id in LEG_IN:  # a bought option needs only its premium
                return proposal.net_debit_per_lot, "premium (long option)"
            estimate = max(proposal.max_loss_per_lot, abs(proposal.net_debit_per_lot)) * Decimal(
                "1.25"
            )
            return estimate, f"estimated (basket margin unavailable: {type(exc).__name__})"

    def _size(
        self, proposal: Proposal, mode: str, portfolio: PortfolioSnapshot, policy: RiskPolicy
    ) -> tuple[int, Decimal, str]:
        risk_lot = proposal.risk_per_lot
        budget = policy.max_trade_risk
        aggregate_room = policy.max_aggregate_risk - portfolio.open_max_loss
        by_risk = int((min(budget, aggregate_room) / risk_lot).to_integral_value(ROUND_FLOOR))
        margin_lot, margin_source = self._margin_per_lot(proposal)
        by_margin = int((portfolio.available_margin / margin_lot).to_integral_value(ROUND_FLOOR))
        depth = min(
            (a.quote.ask_quantity if side is Side.BUY else a.quote.bid_quantity)
            // proposal.lot_size
            for a, side in proposal.legs
        )
        by_freeze = self.config.freeze_quantity // proposal.lot_size
        lots = max(0, min(by_risk, by_margin, depth, by_freeze))
        if proposal.strategy_id in LEG_IN:
            lots = min(lots, self.config.strategy.leg_in_max_lots)
        detail = (
            f"risk {by_risk}, margin {by_margin} ({margin_source} Rs{margin_lot:,.0f}/lot), "
            f"depth {depth}, freeze {by_freeze}"
        )
        risk = RiskEngine(policy.limits)
        while lots >= 1:
            candidate = proposal.to_risk_candidate(lots)
            decision = risk.evaluate(
                candidate,
                portfolio,
                estimated_margin=margin_lot * lots,
                data_is_fresh=True,
                contracts_are_current=True,
                broker_reconciled=True,
            )
            if decision.approved:
                return lots, margin_lot, detail
            detail = f"{decision.reason}; {detail}"
            lots -= 1
        return 0, margin_lot, detail

    def _open(
        self,
        proposal: Proposal,
        lots: int,
        margin_lot: Decimal,
        mode: str,
        policy: RiskPolicy,
        state: MarketState,
        now: datetime,
    ) -> None:
        broker_position_id = None
        prices = {
            a.contract.instrument_token: float(p)
            for (a, _), p in zip(proposal.legs, proposal.prices, strict=True)
        }
        if mode == "live":
            assert self.live is not None
            try:
                broker_position_id, lots, prices = self.live.open(
                    proposal,
                    lots=lots,
                    policy=policy,
                    allocation_pct=self.config.max_margin_utilization,
                )
            except LiveEntryRejected as exc:
                # Nothing was sent: skip this signal, keep trading.
                self.last_arbitration = f"live entry skipped: {exc}"[:200]
                self.book.log_decision(
                    "live_entry_rejected",
                    {"strategy": proposal.strategy_id, "error": str(exc)},
                    now,
                )
                return
            except Exception as exc:
                # Possible residual broker exposure: stop all new entries, leave live mode,
                # and hand over to the operator / emergency close-all (durable ledger).
                self.user_paused = True
                if self.operations is not None:
                    self.operations.paused = True
                    self.operations.mode.force_paper()
                self.notifier.send(
                    f"[LIVE] ENTRY NOT COMPLETED {proposal.strategy_id}: {exc}\n"
                    "Entries PAUSED and mode forced to PAPER. Check Kite positions; "
                    "use /emergency to flatten any residual legs."
                )
                self.book.log_decision(
                    "live_entry_failed", {"strategy": proposal.strategy_id, "error": str(exc)}, now
                )
                return
        legs = [
            BookLeg(
                a.contract.instrument_token,
                a.contract.tradingsymbol,
                a.contract.right.value,
                float(a.contract.strike),
                a.contract.expiry.isoformat(),
                side.value,
                lots * proposal.lot_size,
                prices[a.contract.instrument_token],
                float(a.implied_volatility),
            )
            for a, side in proposal.legs
        ]
        entry_net = sum(
            (
                Decimal(str(leg.entry_price)) * leg.quantity * (1 if leg.side == "BUY" else -1)
                for leg in legs
            ),
            Decimal("0"),
        )
        entry_costs = sum(
            (
                leg_charges(
                    side=leg.side,
                    price=Decimal(str(leg.entry_price)),
                    quantity=leg.quantity,
                    schedule=self.config.charges,
                ).total
                for leg in legs
            ),
            Decimal("0"),
        )
        plan = proposal.exit_plan
        position = BookPosition(
            id=new_position_id(),
            mode=mode,
            strategy_id=proposal.strategy_id,
            status="OPEN",
            opened_at=now.isoformat(),
            session_day=now.astimezone(IST).date().isoformat(),
            lots=lots,
            lot_size=proposal.lot_size,
            entry_net=entry_net,
            max_loss=proposal.max_loss_per_lot * lots,
            max_profit=None
            if proposal.max_profit_per_lot is None
            else proposal.max_profit_per_lot * lots,
            entry_costs=entry_costs,
            legs=legs,
            exit_plan=plan.__dict__.copy(),
            meta={
                "score": round(proposal.score, 1),
                "components": {k: round(v, 1) for k, v in proposal.components.items()},
                "pop": round(proposal.pop, 3),
                "margin": str(margin_lot * lots),
                "regime": state.regime.value,
                "spot": state.spot,
                "bars_held": 0,
                "rationale": proposal.rationale,
                "policy": policy.describe(),
                "multi_expiry": proposal.multi_expiry,
                "stage": "single" if proposal.strategy_id in LEG_IN else None,
                "managed_risk": str(proposal.risk_per_lot * lots),
            },
            broker_position_id=broker_position_id,
        )
        self.book.open_position(position)
        self.book.log_decision(
            "entry",
            {"position": position.id, "strategy": proposal.strategy_id, "lots": lots, "mode": mode},
            now,
        )
        from .reports import name, rs

        be = ", ".join(f"{b:,.0f}" for b in proposal.breakevens) or "n/a (multi-expiry)"
        legs_text = "\n".join(
            f"  {side.value} {a.contract.tradingsymbol} @ {price}"
            for (a, side), price in zip(proposal.legs, proposal.prices, strict=True)
        )
        self.notifier.send(
            f"[{mode.upper()}] ENTRY {name(proposal.strategy_id)} x{lots} lot(s)\n"
            f"SENSEX: {state.spot:,.0f} | regime {state.regime.value.replace('_', ' ')}\n"
            f"Legs:\n{legs_text}\n"
            f"Net {'debit' if entry_net > 0 else 'credit'}: {rs(abs(entry_net))}\n"
            f"Max loss: {rs(position.max_loss)}\n"
            f"Max profit: "
            f"{'unbounded' if position.max_profit is None else rs(position.max_profit)}\n"
            f"Breakeven: {be}\n"
            f"Score: {proposal.score:.1f} | POP: {proposal.pop:.0%}\n"
            f"Why: {proposal.rationale}\n"
            f"Risk budget: {rs(policy.max_trade_risk)} per trade (tier {policy.tier.name})"
        )

    # ---------------------------------------------------------- reporting
    def _daily_summary(self, day: date) -> None:
        from .reports import name, rs

        self._summary_day = day
        lines = []
        for mode in ("paper", "live"):
            closed = self.book.closed_positions(mode=mode, since_day=day.isoformat())
            still_open = self.book.open_positions(mode)
            if not closed and not still_open:
                continue
            total = sum((p.realized_pnl or Decimal("0") for p in closed), Decimal("0"))
            wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
            lines += [
                f"{mode.upper()}",
                f"  Net P&L: {rs(total, 2)}",
                f"  Trades: {len(closed)} ({wins} won)",
            ]
            lines += [
                f"  - {name(p.strategy_id)} x{p.lots}: {rs(p.realized_pnl, 2)} ({p.exit_reason})"
                for p in closed
            ]
            if still_open:
                lines.append(f"  WARNING: {len(still_open)} position(s) still open")
            lines.append("")
        if lines:
            self.notifier.send(f"Daily summary {day:%d %b %Y}\n\n" + "\n".join(lines).rstrip())

    def status_text(self) -> str:
        from . import reports

        return reports.status(self, self.operations)

    def strategies_text(self) -> str:
        from . import reports

        return reports.strategies(self)

    def positions_text(self) -> str:
        from . import reports

        return reports.positions(self)

    def pnl_text(self) -> str:
        from . import reports

        return reports.pnl(self)

    def capital_text(self) -> str:
        from . import reports

        return reports.capital(self)

    def risk_text(self) -> str:
        from . import reports

        return reports.capital(self, risk_view=True)
