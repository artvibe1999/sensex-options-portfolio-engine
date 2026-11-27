"""Execution ports and deterministic paper adapter; live adapter is deliberately absent."""
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .domain import Position, TradingMode


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class ExecutionPhase(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class LegExecutionAction:
    """One leg action in a protective multi-leg execution sequence."""

    leg_index: int
    instrument_token: int
    tradingsymbol: str
    side: str
    quantity: int
    limit_price: Decimal | None = None


def build_leg_execution_plan(
    position: Position, phase: ExecutionPhase
) -> tuple[LegExecutionAction, ...]:
    """Order protective legs before risk-increasing legs.

    Opening: buy protective legs before submitting short legs.
    Closing: buy back original short legs before selling protective longs.
    This is a planning primitive only; it does not submit, retry, or confirm orders.
    """
    if not position.legs:
        raise ValueError("Cannot plan execution for a position without legs")

    tokens: set[int] = set()
    symbols: set[str] = set()
    indexed_legs = list(enumerate(position.legs))
    for _, leg in indexed_legs:
        token = leg.contract.instrument_token
        symbol = leg.contract.tradingsymbol.strip()
        if token <= 0 or not symbol:
            raise ValueError("Execution plan requires valid contract identity")
        if token in tokens or symbol in symbols:
            raise ValueError("Execution plan contains duplicate contract legs")
        tokens.add(token)
        symbols.add(symbol)

    if phase is ExecutionPhase.OPEN:
        ordered = sorted(indexed_legs, key=lambda item: (
            0 if item[1].side.value == "BUY" else 1, item[0]
        ))
    elif phase is ExecutionPhase.CLOSE:
        ordered = sorted(indexed_legs, key=lambda item: (
            0 if item[1].side.value == "SELL" else 1, item[0]
        ))
    else:
        raise ValueError("Unsupported execution phase")

    actions = []
    for index, leg in ordered:
        if phase is ExecutionPhase.OPEN:
            action_side = leg.side.value
        else:
            action_side = "SELL" if leg.side.value == "BUY" else "BUY"
        actions.append(LegExecutionAction(
            leg_index=index,
            instrument_token=leg.contract.instrument_token,
            tradingsymbol=leg.contract.tradingsymbol,
            side=action_side,
            quantity=leg.lots * leg.contract.lot_size,
        ))
    return tuple(actions)


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: OrderStatus
    filled_quantity: int
    average_price: Decimal | None
    message: str = ""


class ExecutionPort(Protocol):
    def submit(self, position: Position) -> list[OrderResult]: ...
    def reconcile(self) -> bool: ...


class PaperExecutionAdapter:
    """Simulation-only adapter. It cannot submit orders to a broker."""

    def __init__(self) -> None:
        self.positions: dict[UUID, Position] = {}
        self.orders: dict[str, OrderResult] = {}

    def submit(self, position: Position) -> list[OrderResult]:
        if position.mode is not TradingMode.PAPER:
            raise ValueError("Paper adapter refuses non-paper positions")
        if position.position_id in self.positions:
            raise ValueError("Duplicate position submission")
        self.positions[position.position_id] = position
        results: list[OrderResult] = []
        for leg in position.legs:
            order_id = str(uuid4())
            message = f"Paper order queued for {leg.contract.tradingsymbol}"
            result = OrderResult(order_id, OrderStatus.ACCEPTED, 0, None, message)
            self.orders[order_id] = result
            results.append(result)
        return results

    def reconcile(self) -> bool:
        return True
