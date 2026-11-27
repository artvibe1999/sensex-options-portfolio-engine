"""Audited, fail-closed emergency flattening for broker-managed SENSEX positions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import Lock
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .domain import Contract, OptionLeg, Position, Side, TradingMode
from .execution import ExecutionPhase
from .live_execution import LiveMultiLegExecutionCoordinator
from .order_lifecycle import OrderLifecycle, OrderState
from .position_lifecycle import PositionLifecycle

IST = ZoneInfo("Asia/Kolkata")
_TERMINAL = {
    OrderState.FILLED.value, OrderState.CANCELLED.value,
    OrderState.REJECTED.value, OrderState.EXPIRED.value,
}


@dataclass(frozen=True)
class EmergencyCloseReport:
    run_id: str
    completed: bool
    halted: bool
    cancelled_entry_orders: tuple[str, ...]
    closed_positions: tuple[str, ...]
    unresolved_orders: tuple[str, ...]
    residual_exposure: tuple[tuple[str, int], ...]
    reasons: tuple[str, ...]

    def to_text(self) -> str:
        status = (
            "COMPLETE — BROKER SENSEX EXPOSURE VERIFIED FLAT"
            if self.completed else "INCOMPLETE — RESIDUAL OR UNRESOLVED EXPOSURE"
        )
        lines = [
            f"EMERGENCY CLOSE-ALL: {status}",
            f"Run: {self.run_id}",
            "New entries: HALTED; mode: PAPER",
            f"Entry orders cancellation-confirmed: {len(self.cancelled_entry_orders)}",
            f"Positions fully closed: {len(self.closed_positions)}",
        ]
        if self.unresolved_orders:
            lines.append("Unresolved orders: " + ", ".join(self.unresolved_orders[:8]))
        if self.residual_exposure:
            lines.append("Broker residuals: " + ", ".join(
                f"{symbol}={quantity}" for symbol, quantity in self.residual_exposure[:8]
            ))
        if self.reasons:
            lines.append("Details: " + "; ".join(self.reasons[:5]))
        return "\n".join(lines)[:3500]


class EmergencyCloseAllService:
    """Halt, cancel-safe-pending, close and independently verify all managed SENSEX risk.

    It never fabricates fills or adopts untracked broker positions into the ledger.
    Any broker/local mismatch, unknown order, stale quote, unconfirmed cancel, or
    failed final reconciliation is returned as residual exposure for manual action.
    """

    def __init__(
        self,
        *,
        operations: Any,
        broker: Any,
        contracts_provider: Any,
        orders: OrderLifecycle,
        positions: PositionLifecycle,
        submission: Any,
        trade_history: Any,
        coordinator: LiveMultiLegExecutionCoordinator,
        store: Any,
        static_ip_confirmed: bool,
        max_quote_age_seconds: int = 2,
    ) -> None:
        if max_quote_age_seconds < 1 or max_quote_age_seconds > 5:
            raise ValueError("Emergency quote age must be between 1 and 5 seconds")
        self._operations = operations
        self._broker = broker
        self._contracts_provider = contracts_provider
        self._orders = orders
        self._positions = positions
        self._submission = submission
        self._trade_history = trade_history
        self._coordinator = coordinator
        self._store = store
        self._static_ip_confirmed = static_ip_confirmed
        self._max_quote_age_seconds = max_quote_age_seconds
        self._lock = Lock()

    def execute(self) -> EmergencyCloseReport:
        # The halt is the first state change, before any broker or storage call.
        self._operations.paused = True
        self._operations.mode.force_paper()
        run_id = str(uuid4())
        if not self._lock.acquire(blocking=False):
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Another emergency close-all run is already active",),
            )
        try:
            self._store.append_event(
                idempotency_key=f"emergency-close:{run_id}:started",
                event_type="EMERGENCY_CLOSE_ALL_STARTED",
                aggregate_id=run_id,
                payload={"run_id": run_id, "mode": "paper", "entries_paused": True},
            )
            report = self._execute_halted(run_id)
            self._store.append_event(
                idempotency_key=f"emergency-close:{run_id}:finished",
                event_type="EMERGENCY_CLOSE_ALL_FINISHED",
                aggregate_id=run_id,
                payload={
                    "run_id": run_id,
                    "completed": report.completed,
                    "unresolved_orders": list(report.unresolved_orders),
                    "residual_exposure": list(report.residual_exposure),
                    "reasons": list(report.reasons),
                },
            )
            return report
        except Exception as exc:
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                (f"Emergency workflow stopped safely: {type(exc).__name__}",),
            )
        finally:
            self._operations.paused = True
            self._operations.mode.force_paper()
            self._lock.release()

    def _execute_halted(self, run_id: str) -> EmergencyCloseReport:
        reasons: list[str] = []
        cancelled: list[str] = []
        closed: list[str] = []
        unresolved: list[str] = []
        if not self._static_ip_confirmed:
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Kite static order-placement IP is not confirmed; broker order actions refused",),
            )
        if not self._store.health_check():
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Durable portfolio storage health check failed",),
            )
        if not self._broker.is_authenticated():
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Kite authentication is unavailable",),
            )

        contracts = self._contracts_provider()
        if not isinstance(contracts, list) or not contracts:
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Current BFO SENSEX contract master is unavailable",),
            )
        by_token = {item.instrument_token: item for item in contracts}
        by_symbol = {item.tradingsymbol: item for item in contracts}
        if len(by_token) != len(contracts) or len(by_symbol) != len(contracts):
            return EmergencyCloseReport(
                run_id, False, True, (), (), (), (),
                ("Current BFO SENSEX contract master contains duplicate identities",),
            )

        # Recover first, cancel only correlated opening intents, and never cancel a
        # protective close already working at the broker.
        for order in self._orders.active_snapshots():
            if str(order.get("mode", "")).lower() != "live":
                continue
            order_id = str(order["order_id"])
            phase = self._order_phase(order_id)
            if phase is None:
                unresolved.append(order_id)
                reasons.append(f"Live order {order_id} has no recognised OPEN/CLOSE phase")
                continue
            try:
                current = self._orders.snapshot(order_id)
                if current is None:
                    unresolved.append(order_id)
                    continue
                state = OrderState(current["state"])
                if state in {OrderState.CREATED, OrderState.VALIDATED} and phase == "OPEN":
                    self._orders.transition(
                        order_id=order_id,
                        new_state=OrderState.REJECTED,
                        update_id=f"emergency-not-submitted:{run_id}",
                        message="Emergency halt prevented an unsubmitted entry intent",
                    )
                    cancelled.append(order_id)
                    continue
                if state is OrderState.VALIDATED and phase == "CLOSE":
                    # A durable, not-yet-submitted close intent is safe to resume
                    # through the coordinator after the portfolio preflight.
                    continue
                self._submission.refresh_order(order_id)
                self._poll_order(order_id)
                current = self._orders.snapshot(order_id)
                if current is None:
                    unresolved.append(order_id)
                    continue
                state = OrderState(current["state"])
                if phase == "OPEN" and state in {
                    OrderState.ACKNOWLEDGED, OrderState.OPEN, OrderState.PARTIALLY_FILLED,
                }:
                    self._submission.cancel_once(order_id)
                    self._submission.refresh_order(order_id)
                    self._poll_order(order_id)
                    self._submission.refresh_order(order_id)
                    self._poll_order(order_id)
                    current = self._orders.snapshot(order_id)
                    state = OrderState(current["state"]) if current else OrderState.UNKNOWN
                if state in _TERMINAL or current.get("cancel_confirmed", False):
                    if phase == "OPEN" and (
                        state in {
                            OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED,
                        }
                        or current.get("cancel_confirmed", False)
                    ):
                        cancelled.append(order_id)
                else:
                    unresolved.append(order_id)
                    reasons.append(
                        f"Order {order_id} remains {state.value}; no replacement or close submitted"
                    )
            except Exception as exc:
                unresolved.append(order_id)
                reasons.append(f"Order {order_id} recovery/cancel failed: {type(exc).__name__}")

        # Re-ingest every current-session trade after cancellation to capture races.
        try:
            self._trade_history.poll_current_session(session_date=datetime.now(IST).date())
            if self._trade_history.has_pending_receipts():
                unresolved.append("BROKER_FILL_PROJECTION")
                reasons.append("Broker fill inbox has unprojected receipts")
        except Exception as exc:
            unresolved.append("BROKER_FILL_BACKFILL")
            reasons.append(f"Current-session trade backfill failed: {type(exc).__name__}")

        if unresolved:
            return self._report(
                run_id, cancelled, closed, unresolved, (), reasons, completed=False
            )

        # Reconcile signed net quantities before issuing any close order. This
        # prevents closing stale planned quantities or touching manual/untracked risk.
        preflight_residuals, preflight_issues = self._reconcile_exposure(
            contracts, require_no_open_orders=True
        )
        if preflight_issues:
            reasons.extend(preflight_issues)
            return self._report(
                run_id, cancelled, closed, unresolved, preflight_residuals,
                reasons, completed=False,
            )

        snapshots = [
            item for item in self._positions.active_snapshots()
            if str(item.get("mode", "")).lower() == "live"
        ]
        for snapshot in snapshots:
            position_id = str(snapshot["position_id"])
            remaining = [
                int(leg["remaining_quantity"]) for leg in snapshot["legs"]
            ]
            if not any(remaining):
                self._positions.finalize_flat(
                    position_id=position_id, update_id=f"emergency-flat:{run_id}"
                )
                closed.append(position_id)
                continue
            try:
                position = self._position_from_snapshot(snapshot, by_token)
                result = self._coordinator.execute(position, ExecutionPhase.CLOSE)
                if result.completed:
                    closed.append(position_id)
                else:
                    reasons.append(
                        f"Position {position_id} not fully closed: {result.reason}; "
                        f"residual={result.residual_exposure}"
                    )
            except Exception as exc:
                reasons.append(f"Position {position_id} close failed: {type(exc).__name__}")

        # A final fresh fill backfill and broker snapshot are mandatory. A local
        # CLOSED projection alone is never accepted as proof of broker flatness.
        try:
            self._trade_history.poll_current_session(session_date=datetime.now(IST).date())
            if self._trade_history.has_pending_receipts():
                unresolved.append("BROKER_FILL_PROJECTION")
                reasons.append("Final broker fill projection is incomplete")
        except Exception as exc:
            unresolved.append("FINAL_FILL_BACKFILL")
            reasons.append(f"Final trade-history backfill failed: {type(exc).__name__}")

        residuals, final_issues = self._reconcile_exposure(
            contracts, require_no_open_orders=True
        )
        reasons.extend(final_issues)
        completed = not residuals and not final_issues and not unresolved
        return self._report(
            run_id, cancelled, closed, unresolved, residuals, reasons,
            completed=completed,
        )

    def _poll_order(self, order_id: str) -> None:
        poll = getattr(self._trade_history, "poll", None)
        if not callable(poll):
            raise TypeError("Trade-history ingestor must expose poll(order_ids)")
        poll([order_id])
        recover = getattr(self._trade_history, "recover_pending", None)
        if callable(recover):
            recover()
        if self._trade_history.has_pending_receipts():
            raise RuntimeError("Broker fill projection has pending receipts")

    def _reconcile_exposure(
        self, contracts: list[Contract], *, require_no_open_orders: bool
    ) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...]]:
        issues: list[str] = []
        broker_positions = self._broker.positions()
        if not isinstance(broker_positions, dict) or not isinstance(
            broker_positions.get("net"), list
        ):
            return (), ("Kite returned a malformed net-position snapshot",)
        actual: dict[int, int] = {}
        symbol_by_token = {item.instrument_token: item.tradingsymbol for item in contracts}
        token_by_symbol = {item.tradingsymbol: item.instrument_token for item in contracts}
        for row in broker_positions["net"]:
            if not isinstance(row, dict):
                issues.append("Kite returned a malformed position row")
                continue
            exchange = str(row.get("exchange", "")).upper()
            if exchange and exchange != "BFO":
                continue
            symbol = str(row.get("tradingsymbol", ""))
            token_value = row.get("instrument_token")
            try:
                token = int(token_value) if token_value is not None else token_by_symbol.get(symbol)
                quantity = int(row.get("quantity", 0))
            except (TypeError, ValueError):
                issues.append(f"Invalid broker quantity for {symbol or 'unknown contract'}")
                continue
            if token is None:
                if quantity and symbol.startswith("SENSEX"):
                    issues.append(
                        f"Unmapped non-zero SENSEX broker position: {symbol or token_value}"
                    )
                continue
            if token not in symbol_by_token:
                if quantity and symbol.startswith("SENSEX"):
                    issues.append(
                        f"SENSEX position token {token} is absent from current contract master"
                    )
                continue
            actual[token] = actual.get(token, 0) + quantity

        expected: dict[int, int] = {}
        for snapshot in self._positions.active_snapshots():
            if str(snapshot.get("mode", "")).lower() != "live":
                continue
            for leg in snapshot["legs"]:
                token = int(leg["instrument_token"])
                quantity = int(leg["remaining_quantity"])
                signed = quantity if str(leg["side"]).upper() == "BUY" else -quantity
                expected[token] = expected.get(token, 0) + signed
        residuals = tuple(
            (symbol_by_token.get(token, str(token)), actual.get(token, 0))
            for token in sorted(set(actual) | set(expected))
            if actual.get(token, 0) != 0
        )
        for token in sorted(set(actual) | set(expected)):
            if actual.get(token, 0) != expected.get(token, 0):
                issues.append(
                    f"Broker/local quantity mismatch for {symbol_by_token.get(token, token)}: "
                    f"broker={actual.get(token, 0)}, ledger={expected.get(token, 0)}"
                )

        if require_no_open_orders:
            broker_orders = self._broker.orders()
            if not isinstance(broker_orders, list):
                issues.append("Kite returned a malformed order book")
            else:
                for row in broker_orders:
                    if not isinstance(row, dict):
                        issues.append("Kite returned a malformed broker order row")
                        continue
                    exchange = str(row.get("exchange", "")).upper()
                    symbol = str(row.get("tradingsymbol", ""))
                    if exchange != "BFO" or (
                        symbol not in token_by_symbol and not symbol.startswith("SENSEX")
                    ):
                        continue
                    if symbol not in token_by_symbol:
                        issues.append(
                            f"Unmapped SENSEX broker order {row.get('order_id', 'UNKNOWN')}"
                        )
                        continue
                    state = str(row.get("status", "")).upper()
                    if state not in {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}:
                        issues.append(
                            f"Unresolved BFO SENSEX broker order {row.get('order_id', 'UNKNOWN')} "
                            f"({state or 'UNKNOWN'}); manual correlation required"
                        )
        return residuals, tuple(issues)

    def _position_from_snapshot(
        self, snapshot: dict[str, Any], contracts: dict[int, Contract]
    ) -> Position:
        legs: list[OptionLeg] = []
        for item in snapshot["legs"]:
            token = int(item["instrument_token"])
            contract = contracts.get(token)
            if contract is None or contract.tradingsymbol != str(item["tradingsymbol"]):
                raise ValueError(
                    "Position contract is absent from refreshed SENSEX contract master"
                )
            quantity = int(item["quantity"])
            if quantity <= 0 or quantity % contract.lot_size:
                raise ValueError("Position plan is not a whole number of current contract lots")
            legs.append(OptionLeg(
                contract=contract,
                side=Side(str(item["side"]).upper()),
                lots=quantity // contract.lot_size,
            ))
        return Position(
            position_id=UUID(str(snapshot["position_id"])),
            strategy_id=str(snapshot["strategy_id"]),
            legs=tuple(legs),
            mode=TradingMode.LIVE,
            opened_at=datetime.now(IST),
            status="CLOSING",
        )

    def _report(
        self,
        run_id: str,
        cancelled: list[str],
        closed: list[str],
        unresolved: list[str],
        residuals: tuple[tuple[str, int], ...],
        reasons: list[str],
        *,
        completed: bool,
    ) -> EmergencyCloseReport:
        return EmergencyCloseReport(
            run_id=run_id,
            completed=completed,
            halted=True,
            cancelled_entry_orders=tuple(dict.fromkeys(cancelled)),
            closed_positions=tuple(dict.fromkeys(closed)),
            unresolved_orders=tuple(dict.fromkeys(unresolved)),
            residual_exposure=residuals,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _order_phase(order_id: str) -> str | None:
        if ":OPEN:" in order_id:
            return "OPEN"
        if ":CLOSE:" in order_id:
            return "CLOSE"
        return None



def build_emergency_limit_price_provider(broker: Any, *, max_age_seconds: int = 2):
    """Return a fresh, marketable LIMIT price policy for risk-reducing orders."""
    if max_age_seconds < 1 or max_age_seconds > 5:
        raise ValueError("Emergency quote age must be between 1 and 5 seconds")

    def price_for(action: Any) -> Decimal:
        contracts = broker.instruments()
        matches = [
            item for item in contracts
            if item.instrument_token == action.instrument_token
            and item.tradingsymbol == action.tradingsymbol
        ]
        if len(matches) != 1:
            raise ValueError("Emergency close contract is not unique in refreshed BFO master")
        contract = matches[0]
        quotes = broker.quotes([contract.instrument_token])
        quote = quotes.get(contract.instrument_token) if isinstance(quotes, dict) else None
        if quote is None:
            raise ValueError("Emergency close quote is unavailable")
        from .market_data import validate_quote

        quality = validate_quote(
            quote,
            now=datetime.now().astimezone(),
            max_age_seconds=max_age_seconds,
            max_spread_pct=Decimal("0.03"),
            min_top_depth=1,
            min_volume=1,
        )
        if not quality.valid:
            raise ValueError(f"Emergency close quote rejected: {quality.reason}")
        raw = quote.ask if action.side == "BUY" else quote.bid
        if not raw.is_finite() or raw <= 0:
            raise ValueError("Emergency close quote has no executable bid/ask")
        units = raw / contract.tick_size
        from decimal import ROUND_CEILING, ROUND_FLOOR

        rounding = ROUND_CEILING if action.side == "BUY" else ROUND_FLOOR
        return units.to_integral_value(rounding=rounding) * contract.tick_size

    return price_for
