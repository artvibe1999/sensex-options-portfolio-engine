"""Paper-only sequential multi-leg coordinator.

This module deliberately has no broker adapter. It demonstrates and tests the
fill-gating invariant: the next leg is never submitted until the prior leg is
confirmed fully filled. Any partial, accepted, rejected, malformed, or uncertain
result stops the sequence and leaves the position blocked for reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import Position, TradingMode
from .execution import (
    ExecutionPhase,
    LegExecutionAction,
    OrderResult,
    OrderStatus,
    build_leg_execution_plan,
)


class PaperLegSubmitter(Protocol):
    """Simulation port; intentionally not implemented by the Kite adapter."""

    def submit_leg(
        self, action: LegExecutionAction, *, client_order_key: str
    ) -> OrderResult: ...


@dataclass(frozen=True)
class LegExecutionOutcome:
    action: LegExecutionAction
    result: OrderResult | None
    confirmed_filled: bool
    reason: str


@dataclass(frozen=True)
class SequentialExecutionReport:
    position_id: str
    phase: ExecutionPhase
    outcomes: tuple[LegExecutionOutcome, ...]
    completed: bool
    blocked: bool
    reason: str


class PaperSequentialExecutionCoordinator:
    """Execute a protective sequence in paper mode, gated on exact full fills."""

    def __init__(self, submitter: PaperLegSubmitter) -> None:
        self._submitter = submitter

    def execute(
        self, position: Position, phase: ExecutionPhase
    ) -> SequentialExecutionReport:
        if position.mode is not TradingMode.PAPER:
            raise PermissionError("Sequential coordinator is paper-mode only")
        plan = build_leg_execution_plan(position, phase)
        outcomes: list[LegExecutionOutcome] = []

        for action in plan:
            key = (
                f"paper:{position.position_id}:{phase.value}:"
                f"leg:{action.leg_index}"
            )
            try:
                result = self._submitter.submit_leg(
                    action, client_order_key=key
                )
            except Exception as exc:  # fail closed on adapter uncertainty
                outcomes.append(LegExecutionOutcome(
                    action, None, False,
                    f"Submission outcome is uncertain: {type(exc).__name__}",
                ))
                return SequentialExecutionReport(
                    str(position.position_id), phase, tuple(outcomes),
                    False, True, "Sequence halted; reconcile before retrying",
                )

            valid_fill = (
                result.status is OrderStatus.FILLED
                and result.filled_quantity == action.quantity
                and bool(result.order_id.strip())
                and result.average_price is not None
                and result.average_price.is_finite()
                and result.average_price > 0
            )
            if not valid_fill:
                outcomes.append(LegExecutionOutcome(
                    action, result, False,
                    "Leg is not confirmed fully filled; later legs were not submitted",
                ))
                return SequentialExecutionReport(
                    str(position.position_id), phase, tuple(outcomes),
                    False, True, "Sequence halted; reconcile outstanding exposure",
                )

            outcomes.append(LegExecutionOutcome(
                action, result, True, "Exact full fill confirmed"
            ))

        return SequentialExecutionReport(
            str(position.position_id), phase, tuple(outcomes),
            True, False, "All legs confirmed fully filled",
        )
