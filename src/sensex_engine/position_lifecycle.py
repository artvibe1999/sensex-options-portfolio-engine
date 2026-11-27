"""Durable multi-leg position lifecycle with verified opening and closing fills.

This is a persistence/domain service only. It does not place or cancel broker orders.
A position is never marked CLOSED until every confirmed open leg has matching close fills.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .persistence import SqliteStore


class PositionLifecycle:
    """Append-only position aggregate reconstructed from durable events."""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def create(
        self,
        *,
        position_id: str,
        strategy_id: str,
        mode: str,
        legs: list[dict[str, Any]],
        idempotency_key: str,
    ) -> bool:
        if not all(value.strip() for value in (position_id, strategy_id, mode, idempotency_key)):
            raise ValueError("Position identity, strategy, mode and idempotency key are required")
        if mode.lower() not in {"paper", "live"} or not legs:
            raise ValueError("Position mode or legs are invalid")
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()
        for leg in legs:
            index = leg.get("leg_index")
            token = leg.get("instrument_token")
            quantity = leg.get("quantity")
            side = str(leg.get("side", "")).upper()
            symbol = str(leg.get("tradingsymbol", "")).strip()
            if (not isinstance(index, int) or isinstance(index, bool) or index < 0
                or index in seen or not isinstance(token, int) or token <= 0
                or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0
                or side not in {"BUY", "SELL"} or not symbol):
                raise ValueError("Position contains an invalid or duplicate leg")
            seen.add(index)
            normalized.append({
                "leg_index": index, "instrument_token": token,
                "tradingsymbol": symbol, "side": side, "quantity": quantity,
            })
        normalized.sort(key=lambda item: item["leg_index"])
        return self.store.append_position_event(
            idempotency_key=idempotency_key,
            event_type="POSITION_CREATED",
            aggregate_id=position_id,
            payload={
                "position_id": position_id, "strategy_id": strategy_id,
                "mode": mode.lower(), "status": "PENDING", "legs": normalized,
            },
        )

    def record_leg_fill(
        self,
        *,
        position_id: str,
        leg_index: int,
        phase: str,
        fill_id: str,
        quantity: int,
        price: Decimal,
        filled_at: str,
    ) -> bool:
        if not position_id.strip() or not fill_id.strip() or not filled_at.strip():
            raise ValueError("Position, fill and timestamp identities are required")
        if not isinstance(leg_index, int) or isinstance(leg_index, bool) or leg_index < 0:
            raise ValueError(
                "Leg index must be non-negative"
            )
        phase = phase.upper()
        if (
            phase not in {"OPEN", "CLOSE"}
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
        ):
            raise ValueError(
                "Invalid fill phase or quantity"
            )
        try:
            fill_price = Decimal(price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Fill price must be a valid decimal") from exc
        if not fill_price.is_finite() or fill_price <= 0:
            raise ValueError("Fill price must be positive and finite")
        current = self.snapshot(position_id)
        if current is None:
            raise ValueError("Unknown position")
        key = f"position-fill:{position_id}:{phase}:{fill_id}"
        prior = next(
            (event for event in self.store.events_for(position_id)
             if event["idempotency_key"] == key),
            None,
        )
        if prior is not None:
            old = prior["payload"]
            if (int(old["leg_index"]) != leg_index or int(old["quantity"]) != quantity
                or Decimal(old["price"]) != fill_price or old["filled_at"] != filled_at):
                raise ValueError("Position fill idempotency key conflicts with existing fill")
            return False
        return self.store.append_position_event(
            idempotency_key=key,
            event_type="POSITION_LEG_FILL_RECORDED",
            aggregate_id=position_id,
            expected_status=current["status"],
            fill_phase=phase,
            leg_index=leg_index,
            fill_quantity=quantity,
            payload={
                "position_id": position_id, "leg_index": leg_index,
                "phase": phase, "fill_id": fill_id, "quantity": quantity,
                "price": str(fill_price), "filled_at": filled_at,
            },
        )

    def transition(self, *, position_id: str, status: str, update_id: str) -> bool:
        if not position_id.strip() or not update_id.strip():
            raise ValueError("Position and update identities are required")
        current = self.snapshot(position_id)
        if current is None:
            raise ValueError("Unknown position")
        target = status.upper()
        return self.store.append_position_event(
            idempotency_key=f"position-update:{position_id}:{update_id}",
            event_type="POSITION_STATUS_CHANGED",
            aggregate_id=position_id,
            expected_status=current["status"],
            payload={"position_id": position_id, "status": target},
        )

    def snapshot(self, position_id: str) -> dict[str, Any] | None:
        events = self.store.events_for(position_id)
        if not events or events[0]["event_type"] != "POSITION_CREATED":
            return None
        created = dict(events[0]["payload"])
        legs = {int(leg["leg_index"]): dict(leg) for leg in created["legs"]}
        for leg in legs.values():
            leg.update({
                "opened_quantity": 0, "closed_quantity": 0,
                "open_average_price": None, "close_average_price": None,
                "remaining_quantity": 0,
            })
        status = created["status"]
        for event in events[1:]:
            payload = event["payload"]
            if event["event_type"] == "POSITION_STATUS_CHANGED":
                status = payload["status"]
            elif event["event_type"] == "POSITION_LEG_FILL_RECORDED":
                leg = legs[int(payload["leg_index"])]
                phase = payload["phase"]
                quantity = int(payload["quantity"])
                if phase == "OPEN":
                    leg["opened_quantity"] += quantity
                    price_key = "open_average_price"
                else:
                    leg["closed_quantity"] += quantity
                    price_key = "close_average_price"
                prior_qty = (
                    leg["opened_quantity"] - quantity
                    if phase == "OPEN"
                    else leg["closed_quantity"] - quantity
                )
                prior_avg = Decimal(leg[price_key]) if leg[price_key] is not None else Decimal("0")
                average = (
                    prior_avg * prior_qty + Decimal(payload["price"]) * quantity
                ) / (prior_qty + quantity)
                leg[price_key] = str(average)
                status = payload["status"]
        for leg in legs.values():
            leg["remaining_quantity"] = leg["opened_quantity"] - leg["closed_quantity"]
        created["status"] = status
        created["legs"] = [legs[index] for index in sorted(legs)]
        created["is_fully_open"] = status == "OPEN" and all(
            leg["opened_quantity"] == leg["quantity"] for leg in created["legs"]
        )
        created["is_fully_closed"] = status == "CLOSED" and all(
            leg["remaining_quantity"] == 0 for leg in created["legs"]
        )
        return created
