"""Fail-closed Zerodha Kite order adapter for BFO SENSEX options.

The broker tag is a 20-character correlation hint, not a broker-side
idempotency guarantee. Recovery scans the transient same-day order book and
requires exact tag + contract/order-field agreement. Ambiguity is never retried.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import Lock
from typing import Any

from .broker_submission import BrokerOrderReceipt, BrokerOrderStatus
from .execution import LegExecutionAction
from .kite import KiteReadOnlyAdapter


_OPEN_STATUSES = {
    "OPEN", "OPEN PENDING", "TRIGGER PENDING", "AMO REQ RECEIVED",
    "PUT ORDER REQ RECEIVED", "VALIDATION PENDING",
    "MODIFY VALIDATION PENDING", "MODIFY PENDING", "CANCEL PENDING",
}
_PARTIAL_STATUSES = {"PARTIALLY FILLED"}


class KiteOrderExecutionAdapter:
    """LIMIT-only SENSEX option execution over the existing authenticated Kite client."""

    _rate_lock = Lock()
    _last_order_call = 0.0
    _minimum_order_interval = 0.16

    def __init__(self, market_data: KiteReadOnlyAdapter) -> None:
        self._market_data = market_data
        self._client = market_data._client

    @staticmethod
    def tag_for_client_key(client_order_key: str) -> str:
        if not isinstance(client_order_key, str) or not client_order_key.strip():
            raise ValueError("A non-empty durable client order key is required")
        return "SX" + sha256(client_order_key.encode("utf-8")).hexdigest()[:18]

    def submit_order(
        self, action: LegExecutionAction, *, client_order_key: str
    ) -> BrokerOrderReceipt:
        self._require_authenticated()
        contract = self._current_contract(action)
        price = self._validated_price(action.limit_price, contract.tick_size)
        if action.side not in {"BUY", "SELL"}:
            raise ValueError("Kite order side must be BUY or SELL")
        if action.quantity <= 0 or action.quantity % contract.lot_size:
            raise ValueError("Order quantity must be a positive whole number of current lots")
        tag = self.tag_for_client_key(client_order_key)

        existing = self.find_orders_by_client_key(client_order_key, expected_action=action)
        if existing:
            if len(existing) != 1:
                raise RuntimeError(
                    "Client tag is ambiguous in Kite order book; manual recovery required"
                )
            row = self._find_order_row(tag)
            self._assert_order_matches(row, action=action, contract=contract, tag=tag)
            return existing[0]

        self._throttle_order_call()
        broker_order_id = self._client.place_order(
            variety="regular",
            exchange="BFO",
            tradingsymbol=contract.tradingsymbol,
            transaction_type=action.side,
            quantity=action.quantity,
            product="NRML",
            order_type="LIMIT",
            price=float(price),
            validity="DAY",
            tag=tag,
        )
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("Kite returned an invalid broker order ID")
        return BrokerOrderReceipt(broker_order_id, BrokerOrderStatus.ACCEPTED)

    def find_orders_by_client_key(
        self, client_order_key: str, *, expected_action: LegExecutionAction | None = None
    ) -> tuple[BrokerOrderReceipt, ...]:
        tag = self.tag_for_client_key(client_order_key)
        self._throttle_order_call()
        rows = self._client.orders()
        if not isinstance(rows, list):
            raise ValueError("Kite order book response must be a list")
        matches = [row for row in rows if isinstance(row, dict) and row.get("tag") == tag]
        receipts: list[BrokerOrderReceipt] = []
        contracts = self._market_data.instruments() if matches else []
        for row in matches:
            order_id = row.get("order_id")
            if not isinstance(order_id, str) or not order_id.strip():
                raise ValueError("Kite order-book match has no valid order ID")
            symbol = row.get("tradingsymbol")
            contract_matches = [item for item in contracts if item.tradingsymbol == symbol]
            if row.get("exchange") != "BFO" or len(contract_matches) != 1:
                raise ValueError("Client tag match is not a current BFO SENSEX option")
            contract = contract_matches[0]
            if expected_action is not None:
                self._assert_order_matches(row, action=expected_action, contract=contract, tag=tag)
            else:
                quantity = int(row.get("quantity") or 0)
                if (
                    row.get("transaction_type") not in {"BUY", "SELL"}
                    or quantity <= 0
                    or quantity % contract.lot_size
                ):
                    raise ValueError("Client tag match has invalid side or lot quantity")
            status = self._map_status(row.get("status"))
            receipts.append(BrokerOrderReceipt(
                order_id, status, str(row.get("status_message") or "")[:500]
            ))
        return tuple(receipts)

    def cancel_order(
        self, broker_order_id: str, *, client_order_key: str
    ) -> BrokerOrderReceipt:
        row, _ = self._verified_open_order(broker_order_id, client_order_key)
        self._throttle_order_call()
        returned_id = self._client.cancel_order(
            variety=str(row.get("variety") or "regular"),
            order_id=broker_order_id,
        )
        if returned_id != broker_order_id:
            raise ValueError("Kite cancel response order ID mismatch")
        return BrokerOrderReceipt(
            broker_order_id, BrokerOrderStatus.OPEN,
            "Cancel request accepted; await broker status and fill reconciliation",
        )

    def modify_order(
        self,
        broker_order_id: str,
        *,
        client_order_key: str,
        price: Decimal,
        quantity: int | None = None,
    ) -> BrokerOrderReceipt:
        row, contract = self._verified_open_order(broker_order_id, client_order_key)
        if row.get("order_type") != "LIMIT":
            raise ValueError("Only existing LIMIT orders may be modified by this adapter")
        limit_price = self._validated_price(price, contract.tick_size)
        filled = int(row.get("filled_quantity") or 0)
        requested = int(row.get("quantity") or 0)
        new_quantity = requested if quantity is None else quantity
        if new_quantity <= 0 or new_quantity % contract.lot_size:
            raise ValueError("Modified quantity must be a positive whole number of current lots")
        if new_quantity < filled:
            raise ValueError("Modified quantity cannot be below already-filled quantity")
        self._throttle_order_call()
        returned_id = self._client.modify_order(
            variety=str(row.get("variety") or "regular"),
            order_id=broker_order_id,
            quantity=new_quantity,
            price=float(limit_price),
            order_type="LIMIT",
            validity="DAY",
        )
        if returned_id != broker_order_id:
            raise ValueError("Kite modify response order ID mismatch")
        return BrokerOrderReceipt(
            broker_order_id, BrokerOrderStatus.OPEN,
            "Modify request accepted; await broker status and fill reconciliation",
        )

    def _require_authenticated(self) -> None:
        if not self._market_data.is_authenticated():
            raise PermissionError("Kite authentication is unavailable; order action refused")

    def _current_contract(self, action: LegExecutionAction):
        if action.instrument_token <= 0 or not action.tradingsymbol.strip():
            raise ValueError("A valid SENSEX contract identity is required")
        contracts = self._market_data.instruments()
        matches = [
            item for item in contracts
            if item.instrument_token == action.instrument_token
            and item.tradingsymbol == action.tradingsymbol
        ]
        if len(matches) != 1:
            raise ValueError("Execution contract is not uniquely present in refreshed BFO master")
        return matches[0]

    def _verified_open_order(self, broker_order_id: str, client_order_key: str):
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("Broker order ID is required")
        self._require_authenticated()
        tag = self.tag_for_client_key(client_order_key)
        self._throttle_order_call()
        history = self._client.order_history(broker_order_id)
        rows = history if isinstance(history, list) else [history]
        if not rows or not isinstance(rows[-1], dict):
            raise ValueError("Kite order history is malformed")
        row = rows[-1]
        if row.get("order_id") != broker_order_id or row.get("tag") != tag:
            raise ValueError("Broker order does not match the durable client reference")
        if row.get("exchange") != "BFO":
            raise ValueError("Refusing to modify/cancel a non-BFO order")
        symbol = row.get("tradingsymbol")
        contracts = self._market_data.instruments()
        contract_matches = [item for item in contracts if item.tradingsymbol == symbol]
        if len(contract_matches) != 1:
            raise ValueError("Order contract is not a current SENSEX option in the BFO master")
        contract = contract_matches[0]
        if (
            row.get("instrument_token") is not None
            and int(row["instrument_token"]) != contract.instrument_token
        ):
            raise ValueError("Broker order token does not match the current contract master")
        quantity = int(row.get("quantity") or 0)
        if quantity <= 0 or quantity % contract.lot_size:
            raise ValueError("Broker order quantity is not a whole number of current lots")
        if row.get("transaction_type") not in {"BUY", "SELL"}:
            raise ValueError("Broker order side is invalid")
        if row.get("status") not in _OPEN_STATUSES | _PARTIAL_STATUSES:
            raise ValueError("Only open or partially filled orders may be modified/cancelled")
        return row, contract

    def _find_order_row(self, tag: str) -> dict[str, Any]:
        rows = self._client.orders()
        matches = [row for row in rows if isinstance(row, dict) and row.get("tag") == tag]
        if len(matches) != 1:
            raise RuntimeError("Client tag is not unique in Kite order book")
        return matches[0]

    @staticmethod
    def _assert_order_matches(row, *, action, contract, tag) -> None:
        expected = (
            row.get("tag") == tag
            and row.get("exchange") == "BFO"
            and row.get("tradingsymbol") == contract.tradingsymbol
            and contract.instrument_token == action.instrument_token
            and (
                row.get("instrument_token") is None
                or int(row["instrument_token"]) == action.instrument_token
            )
            and row.get("transaction_type") == action.side
            and int(row.get("quantity") or 0) == action.quantity
        )
        if not expected:
            raise RuntimeError(
                "Existing Kite tag maps to a different order; manual recovery required"
            )

    @staticmethod
    def _map_status(value: object) -> BrokerOrderStatus:
        if not isinstance(value, str):
            raise ValueError("Kite order status is missing")
        status = value.upper()
        if status in _OPEN_STATUSES:
            return BrokerOrderStatus.OPEN
        if status in _PARTIAL_STATUSES:
            return BrokerOrderStatus.PARTIALLY_FILLED
        if status == "COMPLETE":
            return BrokerOrderStatus.FILLED
        if status == "REJECTED":
            return BrokerOrderStatus.REJECTED
        if status == "CANCELLED":
            return BrokerOrderStatus.CANCELLED
        raise ValueError(f"Unsupported Kite order status: {status}")

    @staticmethod
    def _validated_price(value: Decimal | None, tick_size: Decimal) -> Decimal:
        if value is None:
            raise ValueError("A conservative LIMIT price is mandatory; MARKET orders are disabled")
        try:
            price = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Limit price must be a valid decimal") from exc
        if not price.is_finite() or price <= 0 or price % tick_size != 0:
            raise ValueError(
                "Limit price must be positive and aligned to the current contract tick"
            )
        return price

    @classmethod
    def _throttle_order_call(cls) -> None:
        with cls._rate_lock:
            now = time.monotonic()
            wait = cls._minimum_order_interval - (now - cls._last_order_call)
            if wait > 0:
                time.sleep(wait)
            cls._last_order_call = time.monotonic()
