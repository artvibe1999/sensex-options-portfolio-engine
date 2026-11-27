"""Read-only reconciliation of internal multi-leg exposure against broker state."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .broker import BrokerPort
from .domain import Contract, Position, Side


ACTIVE_ORDER_STATUSES = frozenset({
    "OPEN", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
    "TRIGGER PENDING", "AMO REQ RECEIVED", "MODIFY PENDING",
    "MODIFY VALIDATION PENDING", "CANCEL PENDING",
})


@dataclass(frozen=True)
class ReconciliationReport:
    reconciled: bool
    expected_quantities: tuple[tuple[int, int], ...]
    broker_quantities: tuple[tuple[int, int], ...]
    mismatches: tuple[str, ...]
    pending_order_ids: tuple[str, ...]


def _expected_quantities(positions: list[Position]) -> dict[int, int]:
    expected: dict[int, int] = {}
    for position in positions:
        if position.status == "CLOSED":
            continue
        if position.status == "ERROR":
            raise ValueError(f"Internal position {position.position_id} is in ERROR state")
        for leg in position.legs:
            quantity = leg.contract.lot_size * leg.lots
            signed = quantity if leg.side is Side.BUY else -quantity
            token = leg.contract.instrument_token
            expected[token] = expected.get(token, 0) + signed
    return {token: quantity for token, quantity in expected.items() if quantity}


def reconcile_snapshots(
    *,
    contracts: list[Contract],
    internal_positions: list[Position],
    broker_positions: dict[str, Any],
    broker_orders: list[dict[str, Any]],
) -> ReconciliationReport:
    """Compare broker net quantities with all non-closed internal position legs.

    Any unknown non-zero broker position, quantity mismatch, malformed response,
    or pending broker order prevents a reconciled=True result.
    """
    issues: list[str] = []
    expected = _expected_quantities(internal_positions)
    token_by_symbol: dict[str, int] = {}
    for contract in contracts:
        if contract.instrument_token in {value for value in token_by_symbol.values()}:
            issues.append("Duplicate instrument token in current contract master")
        if contract.tradingsymbol in token_by_symbol:
            issues.append("Duplicate tradingsymbol in current contract master")
        token_by_symbol[contract.tradingsymbol] = contract.instrument_token

    if not isinstance(broker_positions, dict) or not isinstance(
        broker_positions.get("net"), list
    ):
        issues.append("Broker positions response is missing a net positions list")
        net_rows: list[Any] = []
    else:
        net_rows = broker_positions["net"]

    actual: dict[int, int] = {}
    for row in net_rows:
        if not isinstance(row, dict):
            issues.append("Malformed broker position row")
            continue
        try:
            quantity_value = row.get("quantity", 0)
            if isinstance(quantity_value, bool):
                raise ValueError
            quantity_decimal = Decimal(str(quantity_value))
            if (
                not quantity_decimal.is_finite()
                or quantity_decimal != quantity_decimal.to_integral_value()
            ):
                raise ValueError
            quantity = int(quantity_decimal)
        except (InvalidOperation, TypeError, ValueError):
            issues.append("Broker position has an invalid net quantity")
            continue
        token_value = row.get("instrument_token")
        try:
            if isinstance(token_value, bool):
                raise ValueError
            token = int(token_value) if token_value is not None else None
        except (TypeError, ValueError):
            token = None
        if token is None:
            symbol = str(row.get("tradingsymbol", ""))
            token = token_by_symbol.get(symbol)
        if token is None:
            if quantity:
                issues.append(
                    f"Unknown non-zero broker position: {row.get('tradingsymbol', 'UNKNOWN')}"
                )
            continue
        actual[token] = actual.get(token, 0) + quantity

    for token in sorted(set(expected) | set(actual)):
        expected_qty = expected.get(token, 0)
        actual_qty = actual.get(token, 0)
        if expected_qty != actual_qty:
            issues.append(
                f"Position quantity mismatch for token {token}: "
                f"internal={expected_qty}, broker={actual_qty}"
            )

    if not isinstance(broker_orders, list):
        issues.append("Broker orders response is malformed")
        broker_orders = []
    pending: list[str] = []
    for order in broker_orders:
        if not isinstance(order, dict):
            issues.append("Malformed broker order row")
            continue
        status = str(order.get("status", "")).strip().upper()
        if status in ACTIVE_ORDER_STATUSES:
            pending.append(str(order.get("order_id") or "UNKNOWN"))
    if pending:
        issues.append("Broker has pending orders requiring reconciliation")

    return ReconciliationReport(
        reconciled=not issues,
        expected_quantities=tuple(sorted(expected.items())),
        broker_quantities=tuple(sorted(actual.items())),
        mismatches=tuple(issues),
        pending_order_ids=tuple(sorted(pending)),
    )


class BrokerReconciliationService:
    """Fetch read-only broker state and fail closed on every adapter error."""

    def reconcile(
        self,
        broker: BrokerPort,
        *,
        contracts: list[Contract],
        internal_positions: list[Position],
    ) -> ReconciliationReport:
        try:
            if not broker.is_authenticated():
                raise RuntimeError("Broker session is not authenticated")
            positions = broker.positions()
            orders = broker.orders()
            return reconcile_snapshots(
                contracts=contracts,
                internal_positions=internal_positions,
                broker_positions=positions,
                broker_orders=orders,
            )
        except Exception as exc:
            return ReconciliationReport(
                reconciled=False,
                expected_quantities=(),
                broker_quantities=(),
                mismatches=(f"Broker reconciliation failed: {type(exc).__name__}",),
                pending_order_ids=(),
            )
