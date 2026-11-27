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
        accounting: Any | None = None,
    ) -> None:
        if max_items < 1 or max_message_chars < 256:
            raise ValueError("Portfolio query output limits are invalid")
        self.positions = positions
        self.orders = orders
        self.accounting = accounting
        self.max_items = max_items
        self.max_message_chars = max_message_chars

    def pnl_text(self) -> str:
        return self._accounting_text("pnl")

    def capital_text(self) -> str:
        return self._accounting_text("capital")

    def risk_text(self) -> str:
        return self._accounting_text("risk")

    def _accounting_text(self, view: str) -> str:
        if self.accounting is None:
            return (
                "Authoritative account accounting is not configured; values are unavailable "
                "and LIVE readiness remains blocked."
            )
        try:
            report = self.accounting.report()
        except Exception:
            return (
                "Account ledger could not be read safely; values are unavailable "
                "and LIVE readiness remains blocked."
            )
        if report is None:
            return (
                "Account equity is unavailable: opening balance or a fresh reconciled broker "
                "mark is missing, stale, or mismatched. LIVE readiness remains blocked."
            )
        if view == "pnl":
            keys = (
                "realized_net_pnl", "fees_and_taxes", "unrealized_pnl",
                "net_liquidation_equity",
            )
        elif view == "capital":
            keys = (
                "opening_equity", "deposits", "withdrawals", "cash_balance",
                "net_liquidation_equity", "available_margin",
            )
        else:
            keys = (
                "open_max_loss", "weekly_drawdown_pct", "monthly_drawdown_pct",
                "open_positions", "entries_today",
            )
        labels = {
            "realized_net_pnl": "Realised net P&L", "fees_and_taxes": "Fees and taxes",
            "cash_balance": "Cash balance", "unrealized_pnl": "Unrealised P&L",
            "net_liquidation_equity": "Net liquidation equity", "opening_equity": "Opening equity",
            "deposits": "Deposits", "withdrawals": "Withdrawals",
            "available_margin": "Available margin",
            "open_max_loss": "Open defined max loss", "weekly_drawdown_pct": "Weekly drawdown %",
            "monthly_drawdown_pct": "Monthly drawdown %", "open_positions": "Open positions",
            "entries_today": "Entries today",
        }
        return self._limit("\\n".join(
            [f"Reconciled account {view.upper()} (INR):"] +
            [f"{labels[key]}: {report[key]}" for key in keys]
        ))

    def positions_text(self) -> str:
        try:
            snapshots = self.positions.active_snapshots()
        except Exception:
            return "Internal position ledger is unavailable. No broker-state claim can be made."
        if not snapshots:
            return (
                "Internal position ledger: no active positions recorded. "
                "This is not broker-confirmed and not a broker reconciliation result."
            )
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
            return (
                "Internal order ledger: no order intents recorded. "
                "This is not broker-confirmed and not a broker order-book result."
            )
        lines = ["Internal order ledger (not broker-confirmed):"]
        for item in snapshots[: self.max_items]:
            lines.append(
                f"{self._safe(item.get('order_id'))} | {self._safe(item.get('state'))} | "
                f"{self._safe(item.get('side'))} {self._safe(item.get('tradingsymbol'))}"
            )
            lines.append(
                f"  filled={self._integer(item.get('filled_quantity'))}/"
                f"{self._integer(item.get('quantity'))} "
                f"remaining={self._integer(item.get('remaining_quantity'))} "
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
        suffix = "\n... output truncated"
        return value[: self.max_message_chars - len(suffix)] + suffix