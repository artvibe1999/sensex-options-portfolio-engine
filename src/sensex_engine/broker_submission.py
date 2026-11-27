"""Durable, fail-closed broker submission boundary.

A submission attempt is durably marked SUBMITTING before the broker call. Once
that marker exists, this service never submits the same intent again. Timeouts,
crashes, malformed replies, and lookup ambiguity require broker-side recovery;
they are never interpreted as permission to retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from .execution import LegExecutionAction
from .order_lifecycle import OrderLifecycle, OrderState


class BrokerOrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class BrokerOrderReceipt:
    broker_order_id: str
    status: BrokerOrderStatus
    message: str = ""


class BrokerSubmissionPort(Protocol):
    """Broker port must support exact client-key lookup for restart recovery."""

    def submit_order(
        self, action: LegExecutionAction, *, client_order_key: str
    ) -> BrokerOrderReceipt: ...

    def find_orders_by_client_key(
        self, client_order_key: str, *, expected_action: LegExecutionAction | None = None
    ) -> tuple[BrokerOrderReceipt, ...]: ...


@dataclass(frozen=True)
class SubmissionOutcome:
    order_id: str
    state: OrderState
    broker_order_id: str | None
    safe_to_advance: bool
    reason: str


class DurableBrokerSubmissionService:
    """One-shot broker submission with durable pre-submit intent and recovery."""

    def __init__(
        self,
        lifecycle: OrderLifecycle,
        broker: BrokerSubmissionPort,
        *,
        live_submission_enabled: bool = False,
    ) -> None:
        self._lifecycle = lifecycle
        self._broker = broker
        self._live_submission_enabled = live_submission_enabled

    def submit_once(
        self, order_id: str, action: LegExecutionAction
    ) -> SubmissionOutcome:
        snapshot = self._require_matching_intent(order_id, action)
        if snapshot["mode"] != "live":
            raise PermissionError("Broker submission service accepts LIVE intents only")
        if not self._live_submission_enabled:
            raise PermissionError("Live broker submission is disabled")
        if snapshot["state"] != OrderState.VALIDATED.value:
            return self._outcome(
                snapshot, False,
                "Intent is not VALIDATED; duplicate or unsafe submission refused",
            )

        client_key = str(snapshot["client_order_key"])
        # The durable state transition is the one-shot claim. Only the process
        # that wins this compare-and-set may cross the broker boundary.
        claimed = self._lifecycle.transition(
            order_id=order_id,
            new_state=OrderState.SUBMITTING,
            update_id=f"submit-claim:{client_key}",
            message="Durable submission claim recorded before broker call",
        )
        if not claimed:
            current = self._lifecycle.snapshot(order_id)
            assert current is not None
            return self._outcome(
                current, False,
                "Submission claim already exists; broker call was not repeated",
            )

        try:
            receipt = self._broker.submit_order(
                action, client_order_key=client_key
            )
        except Exception as exc:
            self._mark_unknown(
                order_id, client_key,
                f"Broker submission outcome uncertain: {type(exc).__name__}",
            )
            current = self._lifecycle.snapshot(order_id)
            assert current is not None
            return self._outcome(
                current, False,
                "Broker outcome is uncertain; lookup and reconcile before any retry",
            )

        if not self._valid_receipt(receipt):
            self._mark_unknown(
                order_id, client_key, "Malformed broker submission response"
            )
            current = self._lifecycle.snapshot(order_id)
            assert current is not None
            return self._outcome(
                current, False,
                "Malformed broker response; lookup and reconcile before any retry",
            )

        if receipt.status is BrokerOrderStatus.REJECTED:
            state = OrderState.REJECTED
        elif receipt.status is BrokerOrderStatus.CANCELLED:
            state = OrderState.CANCELLED
        else:
            state = OrderState.ACKNOWLEDGED
        try:
            self._lifecycle.transition(
                order_id=order_id,
                new_state=state,
                update_id=f"submit-result:{client_key}:{receipt.broker_order_id}",
                broker_order_id=receipt.broker_order_id,
                message=receipt.message or receipt.status.value,
            )
        except Exception as exc:
            # The broker may have accepted the order even when local correlation
            # persistence fails. Never repeat the broker call in this situation.
            self._mark_unknown(
                order_id, client_key,
                f"Broker response received but local correlation failed: {type(exc).__name__}",
            )
            current = self._lifecycle.snapshot(order_id)
            assert current is not None
            return self._outcome(
                current, False,
                "Broker response could not be durably correlated; lookup and reconcile",
            )
        current = self._lifecycle.snapshot(order_id)
        assert current is not None
        return self._outcome(
            current,
            False,
            "Broker acknowledgement recorded; fills must arrive through the fill ledger"
            if state is OrderState.ACKNOWLEDGED
            else "Broker rejected the order",
        )

    def recover(self, order_id: str) -> SubmissionOutcome:
        snapshot = self._lifecycle.snapshot(order_id)
        if snapshot is None:
            raise ValueError("Unknown order intent")
        state = OrderState(snapshot["state"])
        if state not in {
            OrderState.SUBMITTING, OrderState.UNKNOWN,
            OrderState.RECOVERY_REQUIRED,
        }:
            return self._outcome(
                snapshot, False, "Order does not require submission recovery"
            )

        client_key = str(snapshot.get("client_order_key", "")).strip()
        if not client_key:
            return self._outcome(
                snapshot, False, "Missing persisted client key; manual reconciliation required"
            )
        try:
            expected_action = LegExecutionAction(
                leg_index=int(snapshot["leg_index"]),
                instrument_token=int(snapshot["instrument_token"]),
                tradingsymbol=str(snapshot["tradingsymbol"]),
                side=str(snapshot["side"]),
                quantity=int(snapshot["quantity"]),
                limit_price=(
                    Decimal(str(snapshot["limit_price"]))
                    if snapshot.get("limit_price") is not None else None
                ),
            )
            matches = self._broker.find_orders_by_client_key(
                client_key, expected_action=expected_action
            )
        except Exception as exc:
            if state is OrderState.SUBMITTING:
                self._mark_unknown(
                    order_id, client_key,
                    f"Broker lookup failed: {type(exc).__name__}",
                )
                snapshot = self._lifecycle.snapshot(order_id) or snapshot
            return self._outcome(
                snapshot, False, "Broker lookup failed; do not resubmit"
            )

        if not isinstance(matches, tuple) or any(
            not self._valid_receipt(item) for item in matches
        ):
            if state is not OrderState.RECOVERY_REQUIRED:
                self._lifecycle.transition(
                    order_id=order_id,
                    new_state=OrderState.RECOVERY_REQUIRED,
                    update_id=f"malformed-lookup:{client_key}",
                    message="Malformed broker lookup response",
                )
            current = self._lifecycle.snapshot(order_id) or snapshot
            return self._outcome(
                current, False,
                "Malformed broker lookup response; keep blocked for manual reconciliation",
            )

        if len(matches) != 1:
            target = (
                OrderState.RECOVERY_REQUIRED
                if len(matches) > 1 else OrderState.UNKNOWN
            )
            if target is not state:
                self._lifecycle.transition(
                    order_id=order_id,
                    new_state=target,
                    update_id=f"lookup-{len(matches)}:{client_key}",
                    message=(
                        "Multiple broker orders match client key"
                        if matches else "No broker order currently matches client key"
                    ),
                )
            current = self._lifecycle.snapshot(order_id) or snapshot
            return self._outcome(
                current, False,
                "No unique broker match; keep blocked and reconcile manually",
            )

        receipt = matches[0]
        if not self._valid_receipt(receipt):
            return self._outcome(
                snapshot, False, "Malformed broker lookup result; manual reconciliation required"
            )
        if receipt.status is BrokerOrderStatus.REJECTED:
            target = OrderState.REJECTED
        elif receipt.status is BrokerOrderStatus.CANCELLED:
            target = OrderState.CANCELLED
        else:
            target = OrderState.ACKNOWLEDGED
        self._lifecycle.transition(
            order_id=order_id,
            new_state=target,
            update_id=f"recovered:{receipt.broker_order_id}",
            broker_order_id=receipt.broker_order_id,
            message=f"Recovered broker order status: {receipt.status.value}",
        )
        current = self._lifecycle.snapshot(order_id)
        assert current is not None
        return self._outcome(
            current, False,
            "Broker order correlated; fills still require independent fill-event reconciliation",
        )

    def _require_matching_intent(
        self, order_id: str, action: LegExecutionAction
    ) -> dict:
        snapshot = self._lifecycle.snapshot(order_id)
        if snapshot is None:
            raise ValueError("Unknown order intent")
        if (
            snapshot["instrument_token"] != action.instrument_token
            or snapshot["tradingsymbol"] != action.tradingsymbol
            or snapshot["side"] != action.side
            or snapshot["quantity"] != action.quantity
        ):
            raise ValueError("Execution action does not match persisted order intent")
        persisted_price = snapshot.get("limit_price")
        if (
            (persisted_price is None) != (action.limit_price is None)
            or (
                persisted_price is not None
                and Decimal(str(persisted_price)) != Decimal(action.limit_price)
            )
        ):
            raise ValueError("Execution limit price does not match persisted order intent")
        if not snapshot.get("client_order_key"):
            raise ValueError("Persisted client order key is required")
        return snapshot

    @staticmethod
    def _valid_receipt(receipt: object) -> bool:
        return (
            isinstance(receipt, BrokerOrderReceipt)
            and isinstance(receipt.broker_order_id, str)
            and bool(receipt.broker_order_id.strip())
            and isinstance(receipt.status, BrokerOrderStatus)
        )

    def _mark_unknown(self, order_id: str, client_key: str, message: str) -> None:
        current = self._lifecycle.snapshot(order_id)
        if current is None or current["state"] != OrderState.SUBMITTING.value:
            return
        self._lifecycle.transition(
            order_id=order_id,
            new_state=OrderState.UNKNOWN,
            update_id=f"uncertain:{client_key}",
            message=message,
        )

    @staticmethod
    def _outcome(
        snapshot: dict,
        safe_to_advance: bool,
        reason: str,
    ) -> SubmissionOutcome:
        return SubmissionOutcome(
            order_id=str(snapshot["order_id"]),
            state=OrderState(snapshot["state"]),
            broker_order_id=snapshot.get("broker_order_id"),
            safe_to_advance=safe_to_advance,
            reason=reason,
        )
