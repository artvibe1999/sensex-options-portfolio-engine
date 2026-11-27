"""Validated ingestion of broker execution events into durable ledgers.

This module consumes already-received broker fill events. It does not open a
websocket, poll Kite, or infer fills from order acknowledgements.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .fill_coordinator import FillCoordinator


@dataclass(frozen=True)
class BrokerFillEvent:
    broker_order_id: str
    broker_trade_id: str
    instrument_token: int
    tradingsymbol: str
    side: str
    quantity: int
    price: Decimal
    filled_at: str
    phase: str


@dataclass(frozen=True)
class FillIngestionResult:
    internal_order_id: str
    broker_order_id: str
    broker_trade_id: str
    order_event_created: bool
    position_event_created: bool
    order_state: str
    position_status: str


class BrokerFillIngestionService:
    """Resolve broker IDs to durable intents and persist each fill exactly once."""

    def __init__(self, coordinator: FillCoordinator) -> None:
        self._coordinator = coordinator

    def ingest(self, event: BrokerFillEvent) -> FillIngestionResult:
        self._validate(event)
        event_key = f"{event.broker_order_id}:{event.broker_trade_id}"
        store = self._coordinator.orders.store
        payload = {
            "broker_order_id": event.broker_order_id,
            "broker_trade_id": event.broker_trade_id,
            "instrument_token": event.instrument_token,
            "tradingsymbol": event.tradingsymbol,
            "side": event.side.upper(),
            "quantity": event.quantity,
            "price": str(Decimal(event.price)),
            "filled_at": event.filled_at,
            "phase": event.phase.upper(),
        }
        store.receive_broker_fill(event_key=event_key, payload=payload)
        claim = store.claim_broker_fill(event_key=event_key)
        if claim is None:
            receipt = store.broker_fill_receipt(event_key)
            if receipt is not None and receipt["status"] == "PROCESSED":
                prior = dict(receipt["result"])
                prior["order_event_created"] = False
                prior["position_event_created"] = False
                return FillIngestionResult(**prior)
            raise RuntimeError("Broker fill receipt is already being processed")
        return self._process_claimed(event, event_key, int(claim["attempts"]))

    def _process_claimed(
        self, event: BrokerFillEvent, event_key: str, attempt: int
    ) -> FillIngestionResult:
        store = self._coordinator.orders.store
        try:
            result = self._apply(event)
            result_data = {
                "internal_order_id": result.internal_order_id,
                "broker_order_id": result.broker_order_id,
                "broker_trade_id": result.broker_trade_id,
                "order_event_created": result.order_event_created,
                "position_event_created": result.position_event_created,
                "order_state": result.order_state,
                "position_status": result.position_status,
            }
            store.complete_broker_fill(
                event_key=event_key, attempt=attempt, result=result_data
            )
            return result
        except Exception as exc:
            store.fail_broker_fill(
                event_key=event_key, attempt=attempt, error=str(exc)
            )
            raise

    def recover_pending(self, *, limit: int = 100) -> list[FillIngestionResult]:
        """Replay unprocessed durable receipts after restart or transient failure."""
        store = self._coordinator.orders.store
        recovered: list[FillIngestionResult] = []
        for receipt in store.pending_broker_fills(limit=limit):
            payload = receipt["payload"]
            event = BrokerFillEvent(
                broker_order_id=payload["broker_order_id"],
                broker_trade_id=payload["broker_trade_id"],
                instrument_token=int(payload["instrument_token"]),
                tradingsymbol=payload["tradingsymbol"],
                side=payload["side"],
                quantity=int(payload["quantity"]),
                price=Decimal(payload["price"]),
                filled_at=payload["filled_at"],
                phase=payload["phase"],
            )
            event_key = f"{event.broker_order_id}:{event.broker_trade_id}"
            claim = store.claim_broker_fill(event_key=event_key)
            if claim is None:
                continue
            recovered.append(
                self._process_claimed(event, event_key, int(claim["attempts"]))
            )
        return recovered

    def _apply(self, event: BrokerFillEvent) -> FillIngestionResult:
        matches = [
            snapshot
            for snapshot in self._coordinator.orders.all_snapshots()
            if snapshot.get("broker_order_id") == event.broker_order_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "Broker order ID must resolve to exactly one durable internal intent"
            )
        order = matches[0]
        if (
            int(order["instrument_token"]) != event.instrument_token
            or str(order["tradingsymbol"]) != event.tradingsymbol
            or str(order["side"]).upper() != event.side.upper()
        ):
            raise ValueError("Broker fill contract or side does not match durable order intent")
        fill_key = f"{event.broker_order_id}:{event.broker_trade_id}"
        result = self._coordinator.record_fill(
            order_id=str(order["order_id"]),
            fill_id=fill_key,
            quantity=event.quantity,
            price=event.price,
            filled_at=event.filled_at,
            phase=event.phase,
        )
        position = result["position"]
        updated_order = result["order"]
        return FillIngestionResult(
            internal_order_id=str(order["order_id"]),
            broker_order_id=event.broker_order_id,
            broker_trade_id=event.broker_trade_id,
            order_event_created=bool(result["order_event_created"]),
            position_event_created=bool(result["position_event_created"]),
            order_state=str(updated_order["state"]),
            position_status=str(position["status"]),
        )

    @staticmethod
    def _validate(event: BrokerFillEvent) -> None:
        if not isinstance(event, BrokerFillEvent):
            raise TypeError("A BrokerFillEvent instance is required")
        if (
            not isinstance(event.broker_order_id, str)
            or not isinstance(event.broker_trade_id, str)
            or not event.broker_order_id.strip()
            or not event.broker_trade_id.strip()
        ):
            raise ValueError("Broker order and trade IDs are required")
        if (
            not isinstance(event.instrument_token, int)
            or isinstance(event.instrument_token, bool)
            or event.instrument_token <= 0
        ):
            raise ValueError("Broker fill instrument token must be a positive integer")
        if (
            not isinstance(event.tradingsymbol, str)
            or not event.tradingsymbol.strip()
            or not isinstance(event.side, str)
            or event.side.upper() not in {"BUY", "SELL"}
        ):
            raise ValueError("Broker fill contract and side are invalid")
        if (
            not isinstance(event.quantity, int)
            or isinstance(event.quantity, bool)
            or event.quantity <= 0
        ):
            raise ValueError("Broker fill quantity must be a positive integer")
        try:
            price = Decimal(event.price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Broker fill price must be a valid decimal") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Broker fill price must be positive and finite")
        if not isinstance(event.filled_at, str) or not event.filled_at.strip():
            raise ValueError("Broker fill timestamp is required")
        if (
            not isinstance(event.phase, str)
            or event.phase.upper() not in {"OPEN", "CLOSE"}
        ):
            raise ValueError("Broker fill phase must be OPEN or CLOSE")
