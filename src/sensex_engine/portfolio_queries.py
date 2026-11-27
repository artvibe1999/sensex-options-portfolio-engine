"""Read-only Telegram views over durable internal portfolio ledgers."""
from __future__ import annotations

from typing import Any

from .order_lifecycle import OrderLifecycle
from .position_lifecycle import PositionLifecycle


class PortfolioQueryService:
    """Render internal ledger snapshots; never imply they are broker-confirmed."""

    def __init__(
        self, *, positions: PositionLifecycle, orders: OrderLifecycle,
        max_items: int = 12, max_message_chars: int = 3500,
    ) -> None:
        if max_items < 1 or max_message_chars < 256:
            raise ValueError("Portfolio query output limits are invalid")
        self.positions = positions
        self.orders = orders
        self.max_items = max_items
        self.max_message_chars = max_message_chars

    def positions_text(self) -> str:
        try:
            snapshots = self.positions.active_snapshots()
        except Exception:
            return "Internal position ledger is unavailable. No broker-state claim can be made."
        if not snapshots:
            return "Internal position ledger: no active positions recorded. This is not a broker reconciliation result."
        lines = ["Internal position ledger (not broker-confirmed):"]
        for item in snapshots[: self.max_items]:
            lines.append(
                f"{self._safe(item.get('position_id'))} | "
                f"{self._safe(item.get('strategy_id'))} | "
                f"{self._safe(item.get('status'))}"
            )
            for leg in item.get("legs", []):
                lines.append(
                    f"  {self._safe(leg.get('side'))} {self._safe(leg.get('tradingsymbol'))} "
                    f"remaining={self._integer(leg.get('remaining_quantity'))} "
                    f"filled_open={self._integer(leg.get('opened_quantity'))} "
                    f"filled_close={self._integer(leg.get('closed_quantity'))}"
                )
        if len(snapshots) > self.max_items:
            lines.append(f"... {len(snapshots) - self.max_items} more position(s) omitted")
        return self._limit("\n".join(lines))

    def orders_text(self) -> str:
        try:
            ids = self.orders.store.aggregate_ids_for_event("ORDER_INTENT_CREATED")
            snapshots = [self.orders.snapshot(order_id) for order_id in ids]
            snapshots = [item for item in snapshots if item is not None]
        except Exception:
            return "Internal order ledger is unavailable. No broker-state claim can be made."
        snapshots.sort(key=lambda item: str(item.get("order_id", "")), reverse=True)
        if not snapshots:
            return "Internal order ledger: no order intents recorded. This is not a broker order-book result."
        lines = ["Internal order ledger (not broker-confirmed):"]
        for item in snapshots[: self.max_items]:
            lines.append(
                f"{self._safe(item.get('order_id'))} | {self._safe(item.get('state'))} | "
                f"{self._safe(item.get('side'))} {self._safe(item.get('tradingsymbol'))}"
            )
            lines.append(
                f"  filled={self._integer(item.get('filled_quantity'))}/"
                f"{self._integer(item.get('quantity'))} remaining={self._integer(item.get('remaining_quantity'))} "
                f"avg_fill={self._safe(item.get('average_fill_price') or 'n/a')}"
            )
        if len(snapshots) > self.max_items:
            lines.append(f"... {len(snapshots) - self.max_items} more order(s) omitted")
        return self._limit("\n".join(lines))

    @staticmethod
    def _safe(value: Any) -> str:
        text = str(value if value is not None else "n/a")
        return " ".join(text.replace("\n", " ").replace("\r", " ").split())[:100]

    @staticmethod
    def _integer(value: Any) -> str:
        if isinstance(value, bool):
            return "invalid"
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return "invalid"
        return str(number) if number >= 0 else "invalid"

    def _limit(self, value: str) -> str:
        if len(value) <= self.max_message_chars:
            return value
        return value[: self.max_message_chars - 20] + "\n... output truncated"