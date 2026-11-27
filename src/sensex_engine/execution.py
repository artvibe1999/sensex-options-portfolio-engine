"""Execution ports and deterministic paper adapter; live adapter is deliberately absent."""
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .domain import OptionLeg, Position, TradingMode


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


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
            result = OrderResult(order_id, OrderStatus.ACCEPTED, 0, None, "Paper order queued")
            self.orders[order_id] = result
            results.append(result)
        return results

    def reconcile(self) -> bool:
        return True
