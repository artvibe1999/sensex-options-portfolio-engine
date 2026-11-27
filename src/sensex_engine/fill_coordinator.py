"""Idempotent propagation of broker order fills into position exposure.

The order ledger is the source of broker fill facts. Position projection is replayable:
if a process stops after the order event is stored, the same fill can be submitted again
and will still be propagated to the position aggregate.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .order_lifecycle import OrderLifecycle
from .position_lifecycle import PositionLifecycle


class FillCoordinator:
    """Bridge broker order fills to their associated multi-leg position."""

    def __init__(
        self,
        orders: OrderLifecycle,
        positions: PositionLifecycle,
    ) -> None:
        self.orders = orders
        self.positions = positions

    def record_fill(
        self,
        *,
        order_id: str,
        fill_id: str,
        quantity: int,
        price: Decimal,
        filled_at: str,
        phase: str,
    ) -> dict[str, Any]:
        order = self.orders.snapshot(order_id)
        if order is None:
            raise ValueError("Unknown order intent")
        position_id = str(order["position_id"])
        leg_index = int(order["leg_index"])
        position = self.positions.snapshot(position_id)
        if position is None:
            raise ValueError("Order references an unknown position")
        leg = next(
            (item for item in position["legs"] if int(item["leg_index"]) == leg_index),
            None,
        )
        if leg is None or int(leg["instrument_token"]) != int(order["instrument_token"]):
            raise ValueError("Order contract does not match its position leg")
        normalized_phase = phase.upper()
        if normalized_phase not in {"OPEN", "CLOSE"}:
            raise ValueError("Fill phase must be OPEN or CLOSE")
        expected_side = str(leg["side"]).upper()
        if normalized_phase == "CLOSE":
            expected_side = "SELL" if expected_side == "BUY" else "BUY"
        if str(order["side"]).upper() != expected_side:
            raise ValueError("Order side does not match the requested position fill phase")
        order_quantity = int(order["quantity"])
        if order_quantity > int(leg["quantity"]):
            raise ValueError("Order quantity exceeds planned position leg quantity")
        if normalized_phase == "CLOSE":
            remaining = int(leg["opened_quantity"]) - int(leg["closed_quantity"])
            if remaining <= 0 or order_quantity > remaining:
                raise ValueError("Close order quantity exceeds confirmed remaining exposure")
        elif int(leg["opened_quantity"]) + order_quantity > int(leg["quantity"]):
            raise ValueError("Opening order quantity exceeds unfilled planned exposure")
        # Persist broker fact first. If projection fails or process stops, replaying this
        # exact fill is safe: the order ledger deduplicates it and projection is retried.
        order_created = self.orders.record_fill(
            order_id=order_id,
            fill_id=fill_id,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
        )
        position_created = self.positions.record_leg_fill(
            position_id=position_id,
            leg_index=leg_index,
            phase=normalized_phase,
            fill_id=f"{order_id}:{fill_id}",
            quantity=quantity,
            price=price,
            filled_at=filled_at,
        )
        return {
            "order_event_created": order_created,
            "position_event_created": position_created,
            "order": self.orders.snapshot(order_id),
            "position": self.positions.snapshot(position_id),
        }
