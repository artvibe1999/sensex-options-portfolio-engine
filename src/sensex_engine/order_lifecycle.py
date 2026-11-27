"""Durable, broker-agnostic order-intent and fill lifecycle.

This module records lifecycle facts only. It deliberately does not submit broker orders.
State transitions and fill-capacity checks are serialized with SQLite BEGIN IMMEDIATE
transactions across store instances; broker execution still requires a separate recovery design.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Any

from .persistence import SqliteStore


class OrderState(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


_TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
_ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.VALIDATED, OrderState.REJECTED},
    OrderState.VALIDATED: {OrderState.SUBMITTING, OrderState.REJECTED},
    OrderState.SUBMITTING: {
        OrderState.ACKNOWLEDGED, OrderState.OPEN, OrderState.PARTIALLY_FILLED,
        OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
        OrderState.UNKNOWN, OrderState.RECOVERY_REQUIRED,
    },
    OrderState.ACKNOWLEDGED: {
        OrderState.OPEN, OrderState.PARTIALLY_FILLED, OrderState.FILLED,
        OrderState.CANCEL_PENDING, OrderState.REJECTED, OrderState.UNKNOWN,
        OrderState.RECOVERY_REQUIRED,
    },
    OrderState.OPEN: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING,
        OrderState.CANCELLED, OrderState.EXPIRED, OrderState.UNKNOWN,
        OrderState.RECOVERY_REQUIRED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING,
        OrderState.CANCELLED, OrderState.EXPIRED, OrderState.UNKNOWN,
        OrderState.RECOVERY_REQUIRED,
    },
    OrderState.CANCEL_PENDING: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED,
        OrderState.UNKNOWN, OrderState.RECOVERY_REQUIRED,
    },
    # A cancel acknowledgement ends the unfilled remainder, but broker fill events
    # can race with that acknowledgement and arrive afterward.
    OrderState.CANCELLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED},
    OrderState.UNKNOWN: {
        OrderState.ACKNOWLEDGED, OrderState.OPEN, OrderState.PARTIALLY_FILLED,
        OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
        OrderState.RECOVERY_REQUIRED,
    },
    OrderState.RECOVERY_REQUIRED: {
        OrderState.ACKNOWLEDGED, OrderState.OPEN, OrderState.PARTIALLY_FILLED,
        OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
        OrderState.UNKNOWN,
    },
    OrderState.FILLED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}


class OrderLifecycle:
    """Append-only order lifecycle ledger backed by the portfolio event store."""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store
        self._lock = RLock()

    def create_intent(
        self,
        *,
        order_id: str,
        position_id: str,
        leg_index: int,
        instrument_token: int,
        tradingsymbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        mode: str,
        idempotency_key: str,
        client_order_key: str | None = None,
    ) -> bool:
        if not all(value.strip() for value in (
            order_id, position_id, tradingsymbol, side, order_type, product, mode,
            idempotency_key,
        )):
            raise ValueError("Order intent identity and contract fields are required")
        if leg_index < 0 or instrument_token <= 0 or quantity <= 0:
            raise ValueError("Order intent leg, token and quantity must be valid")
        if side.upper() not in {"BUY", "SELL"}:
            raise ValueError("Order side must be BUY or SELL")
        if mode.lower() not in {"paper", "live"}:
            raise ValueError("Order mode must be paper or live")
        if client_order_key is None:
            client_order_key = "sx" + sha256(order_id.encode("utf-8")).hexdigest()[:22]
        if not client_order_key.strip() or len(client_order_key) > 64:
            raise ValueError("Client order key must contain 1 to 64 characters")
        with self._lock:
            existing = self.snapshot(order_id)
            if existing is not None:
                raise ValueError("Order intent already exists")
            return self.store.append_order_event(
                idempotency_key=idempotency_key,
                event_type="ORDER_INTENT_CREATED",
                aggregate_id=order_id,
                payload={
                    "order_id": order_id,
                    "position_id": position_id,
                    "leg_index": leg_index,
                    "instrument_token": instrument_token,
                    "tradingsymbol": tradingsymbol,
                    "side": side.upper(),
                    "quantity": quantity,
                    "order_type": order_type.upper(),
                    "product": product.upper(),
                    "mode": mode.lower(),
                    "client_order_key": client_order_key,
                    "state": OrderState.CREATED.value,
                    "filled_quantity": 0,
                },
            )

    def transition(
        self,
        *,
        order_id: str,
        new_state: OrderState,
        update_id: str,
        broker_order_id: str | None = None,
        message: str = "",
    ) -> bool:
        if not order_id.strip() or not update_id.strip():
            raise ValueError("Order and update identities are required")
        with self._lock:
            event_key = f"order-update:{order_id}:{update_id}"
            prior = next(
                (event for event in self.store.events_for(order_id)
                 if event["idempotency_key"] == event_key),
                None,
            )
            if prior is not None:
                payload = prior["payload"]
                if payload["state"] != new_state.value:
                    raise ValueError("Order update idempotency key conflicts with existing update")
                return False
            current = self.snapshot(order_id)
            if current is None:
                raise ValueError("Unknown order intent")
            old_state = OrderState(current["state"])
            if new_state == old_state:
                return False
            if new_state not in _ALLOWED[old_state]:
                raise ValueError(f"Invalid order transition: {old_state} -> {new_state}")
            payload: dict[str, Any] = {
                "order_id": order_id,
                "from_state": old_state.value,
                "state": new_state.value,
                "message": message[:500],
            }
            if broker_order_id:
                payload["broker_order_id"] = broker_order_id
            return self.store.append_order_event(
                idempotency_key=event_key,
                expected_state=old_state.value,
                event_type="ORDER_STATE_CHANGED",
                aggregate_id=order_id,
                payload=payload,
            )

    def record_fill(
        self,
        *,
        order_id: str,
        fill_id: str,
        quantity: int,
        price: Decimal,
        filled_at: str,
    ) -> bool:
        if not order_id.strip() or not fill_id.strip() or not filled_at.strip():
            raise ValueError("Order, fill and timestamp identities are required")
        try:
            fill_price = Decimal(price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Fill price must be a valid decimal") from exc
        if quantity <= 0 or not fill_price.is_finite() or fill_price <= 0:
            raise ValueError("Fill quantity and price must be positive")
        with self._lock:
            current = self.snapshot(order_id)
            if current is None:
                raise ValueError("Unknown order intent")
            key = f"broker-fill:{order_id}:{fill_id}"
            prior = next(
                (event for event in self.store.events_for(order_id)
                 if event["idempotency_key"] == key),
                None,
            )
            if prior is not None:
                old_fill = prior["payload"]
                same_fill = (
                    old_fill["order_id"] == order_id
                    and old_fill["fill_id"] == fill_id
                    and int(old_fill["quantity"]) == quantity
                    and Decimal(old_fill["price"]) == fill_price
                    and old_fill["filled_at"] == filled_at
                )
                if not same_fill:
                    raise ValueError("Fill idempotency key conflicts with existing fill")
                return False
            state = OrderState(current["state"])
            # A cancel acknowledgement is terminal for the remaining quantity, not
            # proof that no earlier execution can still be reported by the broker.
            # Accept a late fill only while cumulative quantity remains below the
            # original request; the existing database guard still prevents overfills.
            if state in _TERMINAL and not (
                state is OrderState.CANCELLED
                and int(current["filled_quantity"]) < int(current["quantity"])
            ):
                raise ValueError("Cannot add a fill to a terminal order")
            total = int(current["filled_quantity"]) + quantity
            requested = int(current["quantity"])
            if total > requested:
                raise ValueError("Cumulative fills exceed requested order quantity")
            new_state = OrderState.FILLED if total == requested else OrderState.PARTIALLY_FILLED
            if new_state != state and new_state not in _ALLOWED[state]:
                raise ValueError(f"Order state cannot accept a fill from {state}")
            payload = {
                "order_id": order_id,
                "fill_id": fill_id,
                "quantity": quantity,
                "price": str(fill_price),
                "filled_at": filled_at,
                "cumulative_filled_quantity": total,
                "state": new_state.value,
            }
            created = self.store.append_order_event(
                idempotency_key=key,
                expected_state=state.value,
                max_quantity=requested,
                fill_quantity=quantity,
                event_type="ORDER_FILL_RECORDED",
                aggregate_id=order_id,
                payload=payload,
            )
            return created

    def snapshot(self, order_id: str) -> dict[str, Any] | None:
        events = self.store.events_for(order_id)
        if not events:
            return None
        created = next(
            (event for event in events if event["event_type"] == "ORDER_INTENT_CREATED"),
            None,
        )
        if created is None:
            return None
        state = dict(created["payload"])
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "ORDER_STATE_CHANGED":
                state["state"] = payload["state"]
                if "broker_order_id" in payload:
                    state["broker_order_id"] = payload["broker_order_id"]
                state["last_message"] = payload.get("message", "")
            elif event["event_type"] == "ORDER_FILL_RECORDED":
                state["filled_quantity"] = payload["cumulative_filled_quantity"]
                state["state"] = payload["state"]
                state["average_fill_price"] = self._average_fill_price(events)
        state["remaining_quantity"] = int(state["quantity"]) - int(state["filled_quantity"])
        return state

    def active_snapshots(self) -> list[dict[str, Any]]:
        """Return non-terminal order projections in stable order."""
        terminal = {
            OrderState.FILLED.value,
            OrderState.CANCELLED.value,
            OrderState.REJECTED.value,
            OrderState.EXPIRED.value,
        }
        snapshots = []
        for order_id in self.store.aggregate_ids_for_event("ORDER_INTENT_CREATED"):
            snapshot = self.snapshot(order_id)
            if snapshot is not None and snapshot["state"] not in terminal:
                snapshots.append(snapshot)
        return sorted(snapshots, key=lambda item: str(item["order_id"]))

    @staticmethod
    def _average_fill_price(events: list[dict[str, Any]]) -> str | None:
        fills = [event["payload"] for event in events
                 if event["event_type"] == "ORDER_FILL_RECORDED"]
        quantity = sum(int(fill["quantity"]) for fill in fills)
        if not quantity:
            return None
        value = sum(
            Decimal(fill["price"]) * int(fill["quantity"]) for fill in fills
        ) / Decimal(quantity)
        return str(value)
