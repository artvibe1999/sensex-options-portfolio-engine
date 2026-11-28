"""Durable, fill-confirmed live multi-leg execution orchestration.

This coordinator is deliberately not wired into the Telegram runtime. It requires
both an explicit coordinator enable flag and a separately enabled durable broker
submission service. Every broker action is a persisted LIMIT intent; later legs
are gated on the fill ledger, not order acknowledgements.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from .broker_submission import DurableBrokerSubmissionService
from .domain import Position, Side, StrategyCandidate, TradingMode
from .execution import ExecutionPhase, LegExecutionAction, build_leg_execution_plan
from .order_lifecycle import OrderLifecycle, OrderState
from .position_lifecycle import PositionLifecycle


@dataclass(frozen=True)
class LiveLegOutcome:
    order_id: str
    action: LegExecutionAction
    state: OrderState
    filled_quantity: int
    requested_quantity: int
    confirmed_filled: bool
    reason: str


@dataclass(frozen=True)
class LiveExecutionReport:
    position_id: str
    phase: ExecutionPhase
    outcomes: tuple[LiveLegOutcome, ...]
    completed: bool
    blocked: bool
    residual_exposure: tuple[tuple[int, int], ...]
    reason: str


class LiveMultiLegExecutionCoordinator:
    """Execute and recover a live position one fill-confirmed leg at a time.

    No order is automatically resubmitted after SUBMITTING. A replacement order is
    created only after the previous order is confirmed CANCELLED/REJECTED and its
    individual trade history has been ingested. Unknown/cancel-pending orders block
    all further actions. Failed openings trigger short-first, then long, residual
    cleanup; any uncertainty leaves the position in ERROR for operator recovery.
    """

    def __init__(
        self,
        *,
        orders: OrderLifecycle,
        positions: PositionLifecycle,
        submission: DurableBrokerSubmissionService,
        trade_history: object,
        limit_price: Callable[[LegExecutionAction], Decimal],
        live_execution_enabled: bool = False,
        max_close_replacements: int = 1,
        entry_sizing: object | None = None,
        entry_allowed: Callable[[], bool] | None = None,
        require_entry_approval: bool = False,
    ) -> None:
        if max_close_replacements < 0 or max_close_replacements > 3:
            raise ValueError("max_close_replacements must be between 0 and 3")
        self._orders = orders
        self._positions = positions
        self._submission = submission
        self._trade_history = trade_history
        self._limit_price = limit_price
        self._enabled = live_execution_enabled
        self._max_close_replacements = max_close_replacements
        self._entry_sizing = entry_sizing
        self._entry_allowed = entry_allowed
        self._require_entry_approval = require_entry_approval
        self._entry_lock = Lock()

    def execute(self, position: Position, phase: ExecutionPhase) -> LiveExecutionReport:
        if phase is ExecutionPhase.OPEN and self._require_entry_approval:
            raise PermissionError("Normal live opening requires sized entry approval")
        return self._execute(position, phase)

    def execute_protected_short(
        self, position: Position, *, protecting_position_id: str,
    ) -> LiveExecutionReport:
        """Open ONE short option leg against an already fully-open, fill-confirmed long.

        Used by the leg-in lock plan (buy first, hedge when in profit). The caller must
        have verified strike/expiry geometry; here the durable ledger must show the
        protective long fully OPEN with remaining quantity >= the short quantity.
        """
        with self._entry_lock:
            if not self._enabled:
                raise PermissionError("Live multi-leg execution is disabled")
            if position.mode is not TradingMode.LIVE or len(position.legs) != 1:
                raise ValueError("A protected short must be a single LIVE leg")
            short = position.legs[0]
            if short.side is not Side.SELL:
                raise ValueError("Protected short leg must be a SELL")
            protect = self._positions.snapshot(protecting_position_id)
            if protect is None or protect["status"] != "OPEN" or not protect["is_fully_open"]:
                raise PermissionError("Protective long position is not fully open")
            quantity = short.lots * short.contract.lot_size
            if not any(
                str(leg["side"]).upper() == "BUY" and int(leg["remaining_quantity"]) >= quantity
                for leg in protect["legs"]
            ):
                raise PermissionError("Short quantity exceeds the confirmed protective long")
            return self._execute(position, ExecutionPhase.OPEN)

    def execute_sized(
        self, candidate: StrategyCandidate, *, max_lots: int, allocation_pct: Decimal,
        stop_fraction: Decimal | None = None,
    ) -> LiveExecutionReport:
        """Normal entry path: serialize approval, sizing and immutable limit execution."""
        with self._entry_lock:
            if (
                not self._enabled or self._entry_sizing is None
                or self._entry_allowed is None or not self._entry_allowed()
            ):
                raise PermissionError("Normal live entry gates are not satisfied")
            extra = {} if stop_fraction is None else {"stop_fraction": stop_fraction}
            approved, prices = self._entry_sizing.approve(
                candidate, max_lots=max_lots, allocation_pct=allocation_pct, **extra,
            )
            if not self._entry_allowed():
                raise PermissionError("Live entry permission changed during approval")
            position = Position.create(approved.strategy_id, approved.legs, TradingMode.LIVE)
            return self._execute(position, ExecutionPhase.OPEN, opening_prices=prices)

    def _execute(
        self, position: Position, phase: ExecutionPhase,
        *, opening_prices: dict[int, Decimal] | None = None,
    ) -> LiveExecutionReport:
        if not self._enabled:
            raise PermissionError("Live multi-leg execution is disabled")
        if position.mode is not TradingMode.LIVE:
            raise PermissionError("Live coordinator accepts LIVE positions only")
        if phase not in {ExecutionPhase.OPEN, ExecutionPhase.CLOSE}:
            raise ValueError("Unsupported execution phase")
        self._assert_fill_projection_healthy()
        position_id = str(position.position_id)
        if phase is ExecutionPhase.OPEN:
            self._ensure_position(position)
            return self._open(position, position_id, opening_prices=opening_prices)
        return self._close(position, position_id)

    def _ensure_position(self, position: Position) -> None:
        position_id = str(position.position_id)
        legs = [
            {
                "leg_index": index,
                "instrument_token": leg.contract.instrument_token,
                "tradingsymbol": leg.contract.tradingsymbol,
                "side": leg.side.value,
                "quantity": leg.lots * leg.contract.lot_size,
            }
            for index, leg in enumerate(position.legs)
        ]
        existing = self._positions.snapshot(position_id)
        if existing is None:
            self._positions.create(
                position_id=position_id,
                strategy_id=position.strategy_id,
                mode="live",
                legs=legs,
                idempotency_key=f"position-create:{position_id}",
            )
            return
        if existing["mode"] != "live" or existing["strategy_id"] != position.strategy_id:
            raise ValueError("Position does not match its immutable durable plan")
        actual_legs = existing["legs"]
        if len(actual_legs) != len(legs):
            raise ValueError("Position leg count differs from its immutable durable plan")
        for expected, actual in zip(legs, actual_legs, strict=True):
            if any(actual[key] != expected[key] for key in expected):
                raise ValueError(
                    "Position contract or quantity differs from its immutable durable plan"
                )

    def _open(
        self, position: Position, position_id: str,
        *, opening_prices: dict[int, Decimal] | None = None,
    ) -> LiveExecutionReport:
        snapshot = self._positions.snapshot(position_id)
        if snapshot is None:
            raise RuntimeError("Durable position was not created")
        if snapshot["status"] == "OPEN" and snapshot["is_fully_open"]:
            return self._report(position_id, ExecutionPhase.OPEN, (), True, False,
                                "Position is already fully open")
        if snapshot["status"] != "PENDING":
            return self._report(position_id, ExecutionPhase.OPEN, (), False, True,
                                "Position is not in a resumable PENDING state")
        outcomes: list[LiveLegOutcome] = []
        for action in build_leg_execution_plan(position, ExecutionPhase.OPEN):
            order_id = f"{position_id}:OPEN:{action.leg_index}:1"
            try:
                if opening_prices is not None:
                    if self._entry_allowed is None or not self._entry_allowed():
                        raise PermissionError("Entry permission withdrawn during execution")
                    action = LegExecutionAction(
                        action.leg_index, action.instrument_token, action.tradingsymbol,
                        action.side, action.quantity, opening_prices[action.instrument_token],
                    )
                outcome = self._execute_order(
                    position_id, order_id, action, ExecutionPhase.OPEN
                )
            except Exception as exc:
                outcome = LiveLegOutcome(
                    order_id, action, OrderState.UNKNOWN, 0, action.quantity, False,
                    f"Execution stopped safely: {type(exc).__name__}",
                )
            outcomes.append(outcome)
            if not outcome.confirmed_filled:
                cleanup = self._unwind_residual(position, position_id)
                outcomes.extend(cleanup)
                current = self._positions.snapshot(position_id)
                residual = self._residual(current)
                completed = current is not None and current["status"] == "CLOSED"
                return LiveExecutionReport(
                    position_id, ExecutionPhase.OPEN, tuple(outcomes), False,
                    not completed, residual,
                    "Opening halted; residual exposure was flattened"
                    if completed else "Opening halted; residual exposure remains blocked",
                )
        current = self._positions.snapshot(position_id)
        completed = bool(current and current["status"] == "OPEN" and current["is_fully_open"])
        return LiveExecutionReport(
            position_id, ExecutionPhase.OPEN, tuple(outcomes), completed, not completed,
            self._residual(current),
            "All opening legs are confirmed fully filled"
            if completed else "Opening fills are incomplete; no later leg may proceed",
        )

    def _close(self, position: Position, position_id: str) -> LiveExecutionReport:
        self._ensure_position(position)
        snapshot = self._positions.snapshot(position_id)
        if snapshot is None:
            raise RuntimeError("Durable position is unavailable")
        if snapshot["status"] == "CLOSED" and snapshot["is_fully_closed"]:
            return self._report(position_id, ExecutionPhase.CLOSE, (), True, False,
                                "Position is already flat")
        if snapshot["status"] not in {"OPEN", "PENDING", "ERROR", "CLOSING"}:
            return self._report(position_id, ExecutionPhase.CLOSE, (), False, True,
                                "Position state does not permit a close sequence")
        unresolved_open_orders = [
            order for order in self._orders.all_snapshots()
            if order.get("position_id") == position_id
            and ":OPEN:" in str(order.get("order_id", ""))
            and order["state"] not in {
                OrderState.FILLED.value, OrderState.CANCELLED.value,
                OrderState.REJECTED.value, OrderState.EXPIRED.value,
            }
            and not order.get("cancel_confirmed", False)
        ]
        if unresolved_open_orders:
            self._mark_position_error(position_id, "Opening order remains unresolved")
            return self._report(
                position_id, ExecutionPhase.CLOSE, (), False, True,
                "Close blocked until every opening order is broker-terminal",
            )
        if snapshot["status"] != "CLOSING":
            self._positions.transition(
                position_id=position_id,
                status="CLOSING",
                update_id=f"live-close-start:{len(self._orders.all_snapshots())}",
            )
        outcomes = self._close_residual_legs(position, position_id)
        current = self._positions.snapshot(position_id)
        completed = bool(current and current["status"] == "CLOSED" and current["is_fully_closed"])
        if not completed and current and current["status"] == "CLOSING":
            self._positions.transition(
                position_id=position_id,
                status="ERROR",
                update_id=f"live-close-incomplete:{len(self._orders.all_snapshots())}",
            )
            current = self._positions.snapshot(position_id)
        return LiveExecutionReport(
            position_id, ExecutionPhase.CLOSE, tuple(outcomes), completed, not completed,
            self._residual(current),
            "All confirmed exposure is closed" if completed
            else "Close sequence incomplete; residual exposure remains blocked",
        )

    def _execute_order(
        self,
        position_id: str,
        order_id: str,
        action: LegExecutionAction,
        phase: ExecutionPhase,
    ) -> LiveLegOutcome:
        snapshot = self._orders.snapshot(order_id)
        if snapshot is None:
            price = self._validated_price(
                action.limit_price if phase is ExecutionPhase.OPEN
                and action.limit_price is not None else self._limit_price(action)
            )
            action = LegExecutionAction(
                action.leg_index, action.instrument_token, action.tradingsymbol,
                action.side, action.quantity, price,
            )
            self._orders.create_intent(
                order_id=order_id,
                position_id=position_id,
                leg_index=action.leg_index,
                instrument_token=action.instrument_token,
                tradingsymbol=action.tradingsymbol,
                side=action.side,
                quantity=action.quantity,
                order_type="LIMIT",
                product="NRML",
                mode="live",
                idempotency_key=f"intent:{order_id}",
                limit_price=price,
            )
            self._orders.transition(
                order_id=order_id, new_state=OrderState.VALIDATED,
                update_id="execution-preflight",
                message="Immutable limit intent validated before broker submission",
            )
        else:
            action = LegExecutionAction(
                action.leg_index, action.instrument_token, action.tradingsymbol,
                action.side, action.quantity, Decimal(str(snapshot["limit_price"])),
            )
            if (
                int(snapshot["quantity"]) != action.quantity
                or int(snapshot["instrument_token"]) != action.instrument_token
                or str(snapshot["tradingsymbol"]) != action.tradingsymbol
                or str(snapshot["side"]) != action.side
            ):
                return self._outcome(order_id, action, snapshot,
                                      "Existing durable intent differs from requested leg")
        state = OrderState(snapshot["state"]) if snapshot else OrderState.VALIDATED
        if state is OrderState.VALIDATED:
            self._submission.submit_once(order_id, action)
        elif state in {
            OrderState.SUBMITTING, OrderState.ACKNOWLEDGED, OrderState.OPEN,
            OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN, OrderState.RECOVERY_REQUIRED,
        }:
            self._submission.refresh_order(order_id)
        else:
            self._poll_fills(order_id)
            return self._outcome(order_id, action, self._orders.snapshot(order_id),
                                 "Existing order is terminal; no duplicate submission")
        self._poll_fills(order_id)
        current = self._orders.snapshot(order_id)
        if current is None:
            raise RuntimeError("Order intent disappeared during execution")
        if (
            current["state"] == OrderState.FILLED.value
            and int(current["filled_quantity"]) == action.quantity
        ):
            return self._outcome(order_id, action, current, "Exact broker trade quantity confirmed")
        state = OrderState(current["state"])
        if state in {OrderState.ACKNOWLEDGED, OrderState.OPEN, OrderState.PARTIALLY_FILLED}:
            self._submission.cancel_once(order_id)
            self._poll_fills(order_id)
            self._submission.refresh_order(order_id)
            self._poll_fills(order_id)
            current = self._orders.snapshot(order_id)
        return self._outcome(
            order_id, action, current,
            "Leg not fully filled; cancellation and trade-history reconciliation attempted",
        )

    def _poll_fills(self, order_id: str) -> None:
        snapshot = self._orders.snapshot(order_id)
        if snapshot is None or not snapshot.get("broker_order_id"):
            return
        poll = getattr(self._trade_history, "poll", None)
        if not callable(poll):
            raise TypeError("Trade-history ingestor must expose poll(order_ids)")
        poll([order_id])
        self._assert_fill_projection_healthy()

    def _assert_fill_projection_healthy(self) -> None:
        replay = getattr(self._trade_history, "recover_pending", None)
        if callable(replay):
            replay()
        pending = getattr(self._trade_history, "has_pending_receipts", None)
        if callable(pending) and pending():
            raise RuntimeError("Durable broker fill projection has pending receipts")

    def _unwind_residual(self, position: Position, position_id: str) -> list[LiveLegOutcome]:
        snapshot = self._positions.snapshot(position_id)
        if snapshot is None:
            return []
        unresolved = [
            order for order in self._orders.all_snapshots()
            if order.get("position_id") == position_id
            and order["state"] not in {
                OrderState.FILLED.value, OrderState.CANCELLED.value,
                OrderState.REJECTED.value, OrderState.EXPIRED.value,
            }
            and not order.get("cancel_confirmed", False)
        ]
        if unresolved:
            self._mark_position_error(position_id, "An opening order remains unresolved")
            return []
        if not any(int(leg["remaining_quantity"]) > 0 for leg in snapshot["legs"]):
            if snapshot["status"] == "PENDING":
                self._positions.transition(
                    position_id=position_id, status="ERROR", update_id="open-failed-flat"
                )
            return []
        if snapshot["status"] == "PENDING":
            self._positions.transition(
                position_id=position_id, status="CLOSING", update_id="open-abort-unwind"
            )
        return self._close_residual_legs(position, position_id)

    def _close_residual_legs(
        self, position: Position, position_id: str
    ) -> list[LiveLegOutcome]:
        outcomes: list[LiveLegOutcome] = []
        indexed = list(enumerate(position.legs))
        indexed.sort(key=lambda item: (0 if item[1].side.value == "SELL" else 1, item[0]))
        for index, leg in indexed:
            for _ in range(self._max_close_replacements + 1):
                snapshot = self._positions.snapshot(position_id)
                if snapshot is None:
                    return outcomes
                persisted_leg = next(
                    item for item in snapshot["legs"] if int(item["leg_index"]) == index
                )
                quantity = int(persisted_leg["remaining_quantity"])
                if quantity <= 0:
                    break
                if quantity % leg.contract.lot_size:
                    self._mark_position_error(
                        position_id, "Residual quantity is not a whole contract lot"
                    )
                    return outcomes
                side = "BUY" if leg.side.value == "SELL" else "SELL"
                action = LegExecutionAction(
                    index, leg.contract.instrument_token, leg.contract.tradingsymbol,
                    side, quantity,
                )
                order_id = self._active_or_next_close_order(position_id, index, side)
                try:
                    outcome = self._execute_order(
                        position_id, order_id, action, ExecutionPhase.CLOSE
                    )
                except Exception as exc:
                    self._mark_position_error(position_id, type(exc).__name__)
                    return outcomes
                outcomes.append(outcome)
                if outcome.confirmed_filled:
                    break
                order = self._orders.snapshot(order_id)
                if order is None or (
                    order["state"] not in {
                        OrderState.CANCELLED.value, OrderState.REJECTED.value,
                    }
                    and not order.get("cancel_confirmed", False)
                ):
                    self._mark_position_error(position_id, "Close order remains unresolved")
                    return outcomes
            current = self._positions.snapshot(position_id)
            if current is None or int(next(
                item for item in current["legs"] if int(item["leg_index"]) == index
            )["remaining_quantity"]) > 0:
                self._mark_position_error(position_id, "Close replacement limit reached")
                return outcomes
        return outcomes

    def _active_or_next_close_order(
        self, position_id: str, leg_index: int, side: str
    ) -> str:
        matches = [
            item for item in self._orders.all_snapshots()
            if item.get("position_id") == position_id
            and int(item.get("leg_index", -1)) == leg_index
            and item.get("side") == side
            and item.get("mode") == "live"
            and ":CLOSE:" in str(item.get("order_id", ""))
        ]
        terminal = {
            OrderState.FILLED.value, OrderState.CANCELLED.value,
            OrderState.REJECTED.value, OrderState.EXPIRED.value,
        }
        active = [
            item for item in matches
            if item["state"] not in terminal and not item.get("cancel_confirmed", False)
        ]
        if len(active) > 1:
            raise RuntimeError("Multiple unresolved close orders exist for one position leg")
        if active:
            return str(active[0]["order_id"])
        return f"{position_id}:CLOSE:{leg_index}:{len(matches) + 1}"

    def _mark_position_error(self, position_id: str, reason: str) -> None:
        snapshot = self._positions.snapshot(position_id)
        if snapshot and snapshot["status"] in {"PENDING", "CLOSING"}:
            self._positions.transition(
                position_id=position_id, status="ERROR",
                update_id=f"execution-error:{len(self._orders.all_snapshots())}",
            )

    @staticmethod
    def _validated_price(value: Decimal) -> Decimal:
        try:
            price = Decimal(value)
        except Exception as exc:
            raise ValueError("Execution price must be a valid decimal") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Execution price must be positive and finite")
        return price

    @staticmethod
    def _outcome(order_id: str, action: LegExecutionAction, snapshot: dict | None,
                 reason: str) -> LiveLegOutcome:
        if snapshot is None:
            return LiveLegOutcome(order_id, action, OrderState.UNKNOWN, 0,
                                  action.quantity, False, reason)
        filled = int(snapshot["filled_quantity"])
        state = OrderState(snapshot["state"])
        confirmed = state is OrderState.FILLED and filled == action.quantity
        return LiveLegOutcome(
            order_id, action, state, filled, action.quantity, confirmed, reason
        )

    def _report(self, position_id: str, phase: ExecutionPhase,
                outcomes: tuple[LiveLegOutcome, ...], completed: bool,
                blocked: bool, reason: str) -> LiveExecutionReport:
        return LiveExecutionReport(
            position_id, phase, outcomes, completed, blocked,
            self._residual(self._positions.snapshot(position_id)), reason,
        )

    @staticmethod
    def _residual(snapshot: dict | None) -> tuple[tuple[int, int], ...]:
        if snapshot is None:
            return ()
        return tuple(
            (int(leg["leg_index"]), int(leg["remaining_quantity"]))
            for leg in snapshot["legs"] if int(leg["remaining_quantity"]) > 0
        )
