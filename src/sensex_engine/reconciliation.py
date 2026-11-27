"""Read-only reconciliation of internal multi-leg exposure against broker state."""
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol


class ContractLike(Protocol):
    instrument_token: int
    tradingsymbol: str
    lot_size: int


class LegLike(Protocol):
    contract: ContractLike
    side: Any
    lots: int


class PositionLike(Protocol):
    position_id: Any
    status: str
    legs: tuple[LegLike, ...]


ACTIVE_ORDER_STATUSES = frozenset({
    "OPEN", "PENDING", "PARTIALLY FILLED", "OPEN PENDING",
    "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
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


def _expected_quantities(
    positions: list[PositionLike],
) -> tuple[dict[int, int], list[str]]:
    expected: dict[int, int] = {}
    issues: list[str] = []
    for position in positions:
        if position.status == "CLOSED":
            continue
        if position.status == "ERROR":
            issues.append(f"Internal position {position.position_id} is in ERROR state")
        for leg in position.legs:
            quantity = leg.contract.lot_size * leg.lots
            side = getattr(leg.side, "value", leg.side)
            if quantity <= 0 or side not in {"BUY", "SELL"}:
                issues.append(
                    f"Internal position {position.position_id} has an invalid leg"
                )
                continue
            signed = quantity if side == "BUY" else -quantity
            token = leg.contract.instrument_token
            if token <= 0:
                issues.append(
                    f"Internal position {position.position_id} has an invalid instrument token"
                )
                continue
            expected[token] = expected.get(token, 0) + signed
    return (
        {token: quantity for token, quantity in expected.items() if quantity},
        issues,
    )


def reconcile_snapshots(
    *,
    contracts: list[ContractLike],
    internal_positions: list[PositionLike],
    broker_positions: dict[str, Any],
    broker_orders: list[dict[str, Any]],
) -> ReconciliationReport:
    """Compare broker net quantities with all non-closed internal position legs.

    Any unknown non-zero broker position, quantity mismatch, malformed response,
    or pending broker order prevents a reconciled=True result.
    """
    expected, issues = _expected_quantities(internal_positions)
    token_by_symbol: dict[str, int] = {}
    seen_tokens: set[int] = set()
    for contract in contracts:
        if contract.instrument_token in seen_tokens:
            issues.append("Duplicate instrument token in current contract master")
        seen_tokens.add(contract.instrument_token)
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
        exchange = str(row.get("exchange", "")).strip().upper()
        if exchange and exchange != "BFO":
            continue
        try:
            quantity_value = row.get("quantity", 0)
            if isinstance(quantity_value, bool):
                raise ValueError
            if isinstance(quantity_value, int):
                quantity = quantity_value
            elif (
                isinstance(quantity_value, str)
                and quantity_value.strip().lstrip("-").isdigit()
            ):
                quantity = int(quantity_value.strip())
            else:
                raise ValueError
        except (TypeError, ValueError):
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
        if not status:
            issues.append("Broker order row is missing status")
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

    def reconcile_store(
        self,
        broker: Any,
        *,
        contracts: list[ContractLike],
        position_lifecycle: Any,
        order_lifecycle: Any | None = None,
    ) -> ReconciliationReport:
        """Reconcile broker exposure directly against durable position projections.

        Remaining quantities are used rather than planned quantities, so partial fills
        are represented accurately. Non-OPEN active aggregates still fail closed.
        """
        try:
            snapshots = position_lifecycle.active_snapshots()
            internal: list[Any] = []
            for snapshot in snapshots:
                status = str(snapshot["status"]).upper()
                legs = []
                for leg in snapshot["legs"]:
                    remaining = int(leg["remaining_quantity"])
                    contract = SimpleNamespace(
                        instrument_token=int(leg["instrument_token"]),
                        tradingsymbol=str(leg["tradingsymbol"]),
                        lot_size=1,
                    )
                    legs.append(SimpleNamespace(
                        contract=contract,
                        side=str(leg["side"]).upper(),
                        lots=remaining,
                    ))
                internal.append(SimpleNamespace(
                    position_id=snapshot["position_id"],
                    status=status,
                    legs=tuple(legs),
                    is_fully_open=bool(snapshot.get("is_fully_open", False)),
                ))
            report = self.reconcile(
                broker, contracts=contracts, internal_positions=internal
            )
            lifecycle_issues = tuple(
                f"Internal position {item.position_id} is not fully OPEN (status={item.status})"
                for item in internal
                if item.status != "OPEN" or not item.is_fully_open
            )
            order_issues: list[str] = []
            if order_lifecycle is not None:
                for order in order_lifecycle.active_snapshots():
                    if str(order.get("mode", "")).lower() != "live":
                        continue
                    order_id = str(order.get("order_id", "UNKNOWN"))
                    state = str(order.get("state", "UNKNOWN")).upper()
                    order_issues.append(
                        f"Live internal order {order_id} requires reconciliation (state={state})"
                    )
            all_issues = report.mismatches + lifecycle_issues + tuple(order_issues)
            return ReconciliationReport(
                reconciled=not all_issues,
                expected_quantities=report.expected_quantities,
                broker_quantities=report.broker_quantities,
                mismatches=all_issues,
                pending_order_ids=report.pending_order_ids,
            )
        except Exception as exc:
            return ReconciliationReport(
                reconciled=False,
                expected_quantities=(),
                broker_quantities=(),
                mismatches=(f"Durable position reconciliation failed: {type(exc).__name__}",),
                pending_order_ids=(),
            )

    def reconcile(
        self,
        broker: Any,
        *,
        contracts: list[ContractLike],
        internal_positions: list[PositionLike],
    ) -> ReconciliationReport:
        try:
            if not broker.is_authenticated():
                return ReconciliationReport(
                    reconciled=False,
                    expected_quantities=(),
                    broker_quantities=(),
                    mismatches=("Broker session is not authenticated",),
                    pending_order_ids=(),
                )
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
