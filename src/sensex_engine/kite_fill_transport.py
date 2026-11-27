"""Read-only Kite trade-history polling and durable fill ingestion.

Kite's order book and per-order trade history are used as a reconciliation/backfill
source. This module never places, modifies, or cancels broker orders.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from .broker_fill_ingestion import (
    BrokerFillEvent,
    BrokerFillIngestionService,
    FillIngestionResult,
)
from .order_lifecycle import OrderLifecycle
from .position_lifecycle import PositionLifecycle

IST = ZoneInfo("Asia/Kolkata")


class KiteTradeHistoryIngestor:
    """Poll exact durable live intents and ingest their individual Kite trades.

    Callers supply the internal order IDs eligible for this polling cycle. This
    avoids querying stale prior-session order IDs because Kite's order/trade book
    is a current-day resource. Every returned trade is checked against the
    durable order and position leg before entering the fill inbox.
    """

    def __init__(
        self,
        *,
        client: Any,
        orders: OrderLifecycle,
        positions: PositionLifecycle,
        ingestion: BrokerFillIngestionService,
    ) -> None:
        self._client = client
        self._orders = orders
        self._positions = positions
        self._ingestion = ingestion

    def poll(self, order_ids: Iterable[str]) -> list[FillIngestionResult]:
        """Fetch and ingest all individual trades for the selected live intents."""
        requested = list(order_ids)
        if any(not isinstance(item, str) or not item.strip() for item in requested):
            raise ValueError("Internal order IDs must be non-empty strings")
        if len(requested) != len(set(requested)):
            raise ValueError("Duplicate internal order IDs are not allowed")

        snapshots: list[dict[str, Any]] = []
        for order_id in requested:
            snapshot = self._orders.snapshot(order_id)
            if snapshot is None:
                raise ValueError(f"Unknown internal order intent: {order_id}")
            if snapshot.get("mode") != "live":
                raise PermissionError("Kite trade-history polling accepts LIVE intents only")
            if not snapshot.get("broker_order_id"):
                raise ValueError(f"Order intent {order_id} has no broker order correlation")
            snapshots.append(snapshot)

        results: list[FillIngestionResult] = []
        for snapshot in snapshots:
            broker_order_id = str(snapshot["broker_order_id"])
            rows = self._client.order_trades(broker_order_id)
            if not isinstance(rows, list):
                raise ValueError("Kite order_trades response must be a list")
            phase = self._resolve_phase(snapshot)
            events = [
                self._to_event(row, snapshot=snapshot, phase=phase)
                for row in rows
            ]
            events.sort(key=lambda item: (item.filled_at, item.broker_trade_id))
            for event in events:
                results.append(self._ingestion.ingest(event))
        return results

    def _resolve_phase(self, order: dict[str, Any]) -> str:
        position = self._positions.snapshot(str(order["position_id"]))
        if position is None:
            raise ValueError("Broker order references an unknown durable position")
        legs = [
            leg for leg in position["legs"]
            if int(leg["leg_index"]) == int(order["leg_index"])
        ]
        if len(legs) != 1:
            raise ValueError("Broker order does not resolve to exactly one position leg")
        leg = legs[0]
        if (
            int(leg["instrument_token"]) != int(order["instrument_token"])
            or str(leg["tradingsymbol"]) != str(order["tradingsymbol"])
        ):
            raise ValueError("Broker order contract does not match durable position leg")
        original_side = str(leg["side"]).upper()
        order_side = str(order["side"]).upper()
        if order_side == original_side:
            return "OPEN"
        if order_side == ("SELL" if original_side == "BUY" else "BUY"):
            return "CLOSE"
        raise ValueError("Broker order side cannot be resolved to OPEN or CLOSE")

    def _to_event(
        self, row: Any, *, snapshot: dict[str, Any], phase: str
    ) -> BrokerFillEvent:
        if not isinstance(row, dict):
            raise ValueError("Kite trade-history row must be an object")
        broker_order_id = str(row.get("order_id", ""))
        if broker_order_id != str(snapshot["broker_order_id"]):
            raise ValueError("Kite trade row order ID does not match requested order")
        exchange = str(row.get("exchange", "")).upper()
        if exchange != "BFO":
            raise ValueError("Kite trade row is not a BFO execution")
        trade_id = row.get("trade_id")
        if trade_id is None or not str(trade_id).strip():
            raise ValueError("Kite trade row is missing its exchange trade ID")

        token = self._positive_int(row.get("instrument_token"), "instrument token")
        quantity = self._positive_int(row.get("quantity"), "trade quantity")
        symbol = row.get("tradingsymbol")
        side = str(row.get("transaction_type", "")).upper()
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Kite trade row is missing tradingsymbol")
        if (
            token != int(snapshot["instrument_token"])
            or symbol != str(snapshot["tradingsymbol"])
            or side != str(snapshot["side"]).upper()
        ):
            raise ValueError("Kite trade contract or side differs from durable order intent")

        try:
            price = Decimal(str(row.get("average_price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Kite trade average_price is invalid") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Kite trade average_price must be positive and finite")

        timestamp = self._timestamp(row.get("fill_timestamp"))
        return BrokerFillEvent(
            broker_order_id=broker_order_id,
            broker_trade_id=str(trade_id),
            instrument_token=token,
            tradingsymbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            filled_at=timestamp.isoformat(),
            phase=phase,
        )

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Kite trade {field} must be a positive integer")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.strip().isdigit():
            result = int(value.strip())
        else:
            raise ValueError(f"Kite trade {field} must be a positive integer")
        if result <= 0:
            raise ValueError(f"Kite trade {field} must be a positive integer")
        return result

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError as exc:
                    raise ValueError("Kite trade fill_timestamp is invalid") from exc
        else:
            raise ValueError("Kite trade fill_timestamp is required")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed
