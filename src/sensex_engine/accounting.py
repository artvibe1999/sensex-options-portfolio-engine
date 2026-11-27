"""Durable, cash-flow-aware account ledger and fail-closed portfolio snapshots.

The service deliberately requires an explicit opening balance and reconciled broker
marks. It never assumes the illustrative capital amount in the requirements document.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .capital import adaptive_risk_budget, affordable_lots
from .domain import PortfolioSnapshot
from .filled_risk import filled_position_risk
from .reconciliation import BrokerReconciliationService, ReconciliationReport

ACCOUNT_ID = "account:default"
IST = ZoneInfo("Asia/Kolkata")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal amount") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


class PortfolioAccountingService:
    """Append-only account ledger; broker marks are accepted only after reconciliation."""

    def __init__(self, *, store: Any, currency: str = "INR") -> None:
        self.store = store
        if currency != "INR":
            raise ValueError("This SENSEX account ledger currently supports INR only")
        self.currency = currency

    def _events(self) -> list[dict[str, Any]]:
        return self.store.events_for(ACCOUNT_ID)

    def _append(self, key: str, event_type: str, payload: dict[str, Any]) -> bool:
        if not key.strip():
            raise ValueError("A stable idempotency key is required")
        existing = [
            event for event in self._events()
            if event["idempotency_key"] == f"account:{key}"
        ]
        if existing:
            event = existing[0]
            if event["event_type"] != event_type or event["payload"] != payload:
                raise ValueError("Account ledger idempotency key conflicts with an existing event")
            return False
        return self.store.append_account_event(
            idempotency_key=f"account:{key}",
            event_type=event_type,
            aggregate_id=ACCOUNT_ID,
            payload=payload,
        )

    def set_opening_equity(self, amount: Decimal, *, idempotency_key: str) -> bool:
        amount = _decimal(amount, "opening_equity")
        if amount <= 0:
            raise ValueError("Opening equity must be positive")
        payload = {"amount": str(amount), "currency": self.currency}
        prior = next(
            (e for e in self._events() if e["idempotency_key"] == f"account:{idempotency_key}"),
            None,
        )
        if prior is not None:
            if prior["event_type"] != "ACCOUNT_OPENED" or prior["payload"] != payload:
                raise ValueError("Account ledger idempotency key conflicts with an existing event")
            return False
        if any(e["event_type"] == "ACCOUNT_OPENED" for e in self._events()):
            raise ValueError("Opening equity is immutable once the account ledger is opened")
        return self._append(idempotency_key, "ACCOUNT_OPENED", payload)

    def record_cash_flow(
        self, amount: Decimal, *, direction: str, idempotency_key: str,
        occurred_at: datetime | None = None, reference: str = "",
    ) -> bool:
        amount = _decimal(amount, "cash_flow")
        if amount <= 0 or direction not in {"DEPOSIT", "WITHDRAWAL"}:
            raise ValueError(
                "Cash flow requires a positive amount and DEPOSIT/WITHDRAWAL direction"
            )
        if not any(e["event_type"] == "ACCOUNT_OPENED" for e in self._events()):
            raise RuntimeError("Set the explicit opening equity before recording cash flows")
        timestamp = occurred_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Cash-flow timestamp must be timezone-aware")
        return self._append(idempotency_key, "ACCOUNT_CASH_FLOW", {
            "amount": str(amount), "direction": direction,
            "occurred_at": timestamp.astimezone(UTC).isoformat(),
            "reference": str(reference)[:120],
        })

    def record_trade_result(
        self, *, realized_pnl: Decimal, fees: Decimal, idempotency_key: str,
        occurred_at: datetime | None = None, broker_trade_ids: tuple[str, ...] = (),
    ) -> bool:
        pnl = _decimal(realized_pnl, "realized_pnl")
        charges = _decimal(fees, "fees")
        if charges < 0:
            raise ValueError("Fees and taxes cannot be negative")
        if not any(e["event_type"] == "ACCOUNT_OPENED" for e in self._events()):
            raise RuntimeError("Set the explicit opening equity before recording trading results")
        timestamp = occurred_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Trade-result timestamp must be timezone-aware")
        return self._append(idempotency_key, "ACCOUNT_TRADE_RESULT", {
            "realized_pnl": str(pnl), "fees": str(charges),
            "occurred_at": timestamp.astimezone(UTC).isoformat(),
            "broker_trade_ids": sorted(set(str(x) for x in broker_trade_ids)),
        })

    def invalidate_mark(self) -> None:
        self._append(str(uuid4()), "ACCOUNT_MARK_INVALIDATED", {})

    def record_reconciled_mark(
        self, *, broker_net_equity: Decimal, unrealized_pnl: Decimal,
        available_margin: Decimal, open_max_loss: Decimal, open_positions: int,
        entries_today: int, reconciliation_id: str,
        reconciliation_report: ReconciliationReport,
        marked_at: datetime | None = None,
        actual_cash: Decimal | None = None,
    ) -> bool:
        values = {
            "broker_net_equity": _decimal(broker_net_equity, "broker_net_equity"),
            "unrealized_pnl": _decimal(unrealized_pnl, "unrealized_pnl"),
            "available_margin": _decimal(available_margin, "available_margin"),
            "open_max_loss": _decimal(open_max_loss, "open_max_loss"),
        }
        if not reconciliation_id.strip():
            raise ValueError("Broker reconciliation identity is required")
        if (
            not isinstance(reconciliation_report, ReconciliationReport)
            or reconciliation_report.reconciled is not True
            or reconciliation_report.mismatches
            or reconciliation_report.pending_order_ids
            or reconciliation_report.expected_quantities
            != reconciliation_report.broker_quantities
        ):
            raise ValueError("A clean broker position/order reconciliation is required")
        positions_hash = hashlib.sha256(
            repr(reconciliation_report.broker_quantities).encode("utf-8")
        ).hexdigest()
        if (
            values["broker_net_equity"] <= 0
            or values["available_margin"] < 0
            or values["open_max_loss"] < 0
        ):
            raise ValueError("Broker equity must be positive and risk/margin values non-negative")
        if (
            not isinstance(open_positions, int) or isinstance(open_positions, bool)
            or open_positions < 0
            or not isinstance(entries_today, int) or isinstance(entries_today, bool)
            or entries_today < 0
        ):
            raise ValueError("Position and entry counts must be non-negative integers")
        if open_positions > 0 and values["open_max_loss"] <= 0:
            raise ValueError("Open positions require a positive, defined maximum-loss estimate")
        if open_positions == 0 and values["open_max_loss"] != 0:
            raise ValueError("A flat portfolio cannot report non-zero open maximum loss")
        timestamp = marked_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Equity mark timestamp must be timezone-aware")
        if not any(e["event_type"] == "ACCOUNT_OPENED" for e in self._events()):
            raise RuntimeError("Set the explicit opening equity before recording broker marks")
        key = f"mark:{reconciliation_id}"
        return self._append(key, "ACCOUNT_RECONCILED_MARK", {
            **{k: str(v) for k, v in values.items()},
            "open_positions": open_positions, "entries_today": entries_today,
            "reconciliation_id": reconciliation_id,
            "broker_positions_hash": positions_hash,
            "marked_at": timestamp.astimezone(UTC).isoformat(),
            "actual_cash": str(_decimal(actual_cash, "actual cash"))
            if actual_cash is not None else None,
        })

    def reconcile_and_record_mark(
        self,
        *,
        broker: Any,
        contracts: list[Any],
        position_lifecycle: Any,
        order_lifecycle: Any,
        broker_net_equity: Decimal,
        unrealized_pnl: Decimal,
        available_margin: Decimal,
        open_max_loss: Decimal,
        open_positions: int,
        entries_today: int,
        reconciliation_id: str,
        marked_at: datetime | None = None,
    ) -> ReconciliationReport:
        """Read broker state, reconcile actual fills/orders, then persist a mark.

        A mismatch or unresolved order raises and no equity mark is written.
        Equity and open-risk values must come from independent authoritative
        account/payoff providers; Kite margin alone is not net liquidation equity.
        """
        report = BrokerReconciliationService().reconcile_store(
            broker,
            contracts=contracts,
            position_lifecycle=position_lifecycle,
            order_lifecycle=order_lifecycle,
        )
        if not report.reconciled:
            raise RuntimeError(
                "Broker reconciliation failed: " + "; ".join(report.mismatches)
            )
        derived_loss, derived_count = filled_position_risk(
            position_lifecycle.active_snapshots(), contracts, mode="live",
        )
        if derived_loss != open_max_loss or derived_count != open_positions:
            raise ValueError("Supplied risk disagrees with actual filled exposure")
        self.record_reconciled_mark(
            broker_net_equity=broker_net_equity,
            unrealized_pnl=unrealized_pnl,
            available_margin=available_margin,
            open_max_loss=open_max_loss,
            open_positions=open_positions,
            entries_today=entries_today,
            reconciliation_id=reconciliation_id,
            reconciliation_report=report,
            marked_at=marked_at,
        )
        return report

    def snapshot(
        self, *, now: datetime | None = None,
        max_mark_age: timedelta = timedelta(minutes=2),
    ) -> PortfolioSnapshot | None:
        events = self._events()
        opened = [e for e in events if e["event_type"] == "ACCOUNT_OPENED"]
        marks = [e for e in events if e["event_type"] == "ACCOUNT_RECONCILED_MARK"]
        if len(opened) != 1 or not marks:
            return None
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("Snapshot clock must be timezone-aware")
        latest = max(marks, key=lambda e: e["event_id"])
        if any(e["event_id"] > latest["event_id"] for e in events):
            return None
        mark = latest["payload"]
        marked_at = datetime.fromisoformat(mark["marked_at"])
        if not timedelta(0) <= current - marked_at <= max_mark_age:
            return None
        opening = _decimal(opened[0]["payload"]["amount"], "opening_equity")
        deposits = withdrawals = realized = fees = Decimal("0")
        daily_realized = daily_flows = Decimal("0")
        today = current.astimezone(IST).date()
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "ACCOUNT_CASH_FLOW":
                amount = _decimal(payload["amount"], "cash_flow")
                direction = payload["direction"]
                if direction == "DEPOSIT":
                    deposits += amount
                    signed = amount
                elif direction == "WITHDRAWAL":
                    withdrawals += amount
                    signed = -amount
                else:
                    raise ValueError("Corrupt account cash-flow event")
                event_date = datetime.fromisoformat(payload["occurred_at"]).astimezone(IST).date()
                if event_date == today:
                    daily_flows += signed
            elif event["event_type"] == "ACCOUNT_TRADE_RESULT":
                pnl = _decimal(payload["realized_pnl"], "realized_pnl")
                charge = _decimal(payload["fees"], "fees")
                realized += pnl
                fees += charge
                event_date = datetime.fromisoformat(payload["occurred_at"]).astimezone(IST).date()
                if event_date == today:
                    daily_realized += pnl - charge
        unrealized = _decimal(mark["unrealized_pnl"], "unrealized_pnl")
        equity = opening + deposits - withdrawals + realized - fees + unrealized
        # Broker net liquidation equity is an independent cross-check;
        # it is never silently substituted.
        broker_equity = _decimal(mark["broker_net_equity"], "broker_net_equity")
        if abs(equity - broker_equity) > Decimal("1.00"):
            return None
        cash_flow_rows = [
            (datetime.fromisoformat(e["payload"]["occurred_at"]).astimezone(UTC),
             (Decimal("1") if e["payload"]["direction"] == "DEPOSIT" else Decimal("-1"))
             * _decimal(e["payload"]["amount"], "cash_flow"))
            for e in events if e["event_type"] == "ACCOUNT_CASH_FLOW"
        ]
        mark_rows = sorted(
            (
                datetime.fromisoformat(e["payload"]["marked_at"]).astimezone(UTC),
                _decimal(e["payload"]["broker_net_equity"], "broker_net_equity")
                - sum((flow for flow_time, flow in cash_flow_rows if flow_time <=
                       datetime.fromisoformat(e["payload"]["marked_at"]).astimezone(UTC)),
                      Decimal("0")),
            )
            for e in marks
        )
        current_adjusted_equity = equity - deposits + withdrawals
        adjusted_peak = max((v for _, v in mark_rows), default=current_adjusted_equity)
        # Scale the flow-neutral peak back to current account equity so risk sizing
        # uses actual capital while the drawdown ratio excludes external cash flows.
        peak = (
            equity * adjusted_peak / current_adjusted_equity
            if current_adjusted_equity > 0 else equity
        )
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        def period_drawdown(start: date) -> Decimal:
            period_values = [v for t, v in mark_rows if t.astimezone(IST).date() >= start]
            if not period_values or current_adjusted_equity <= 0:
                return Decimal("0") if current_adjusted_equity > 0 else Decimal("1")
            high = max(period_values)
            return max(
                Decimal("0"), (high - current_adjusted_equity) / high
            ) if high > 0 else Decimal("1")
        return PortfolioSnapshot(
            net_liquidation_equity=equity,
            available_margin=_decimal(mark["available_margin"], "available_margin"),
            open_max_loss=_decimal(mark["open_max_loss"], "open_max_loss"),
            daily_realized_pnl=daily_realized,
            daily_unrealized_pnl=_decimal(mark["unrealized_pnl"], "unrealized_pnl"),
            weekly_drawdown_pct=period_drawdown(week_start),
            monthly_drawdown_pct=period_drawdown(month_start),
            open_positions=int(mark["open_positions"]),
            entries_today=int(mark["entries_today"]),
            deposits_withdrawals_today=daily_flows,
            peak_equity=peak,
        )

    def size_strategy_lots(
        self,
        *,
        strategy_risk_pct: Decimal,
        aggregate_risk_pct: Decimal,
        allocation_pct: Decimal,
        max_loss_per_lot: Decimal,
        estimated_cost_per_lot: Decimal,
        margin_per_lot: Decimal,
        max_lots: int,
        now: datetime | None = None,
    ) -> int:
        """Size whole lots from fresh equity, remaining portfolio risk and margin."""
        snapshot = self.snapshot(now=now)
        if snapshot is None or snapshot.peak_equity is None:
            return 0
        risk_pct = _decimal(strategy_risk_pct, "strategy_risk_pct")
        aggregate_pct = _decimal(aggregate_risk_pct, "aggregate_risk_pct")
        allocation = _decimal(allocation_pct, "allocation_pct")
        if not isinstance(max_lots, int) or isinstance(max_lots, bool) or max_lots <= 0:
            return 0
        if not (
            Decimal("0") < risk_pct <= Decimal("1")
            and Decimal("0") < aggregate_pct <= Decimal("1")
            and Decimal("0") < allocation <= Decimal("1")
        ):
            return 0
        budget = adaptive_risk_budget(
            snapshot.net_liquidation_equity,
            peak_equity=snapshot.peak_equity,
            base_risk_pct=risk_pct,
            base_aggregate_pct=aggregate_pct,
        )
        if not budget.new_entries_allowed:
            return 0
        remaining_risk = budget.max_aggregate_risk - snapshot.open_max_loss
        risk_allowance = min(budget.max_trade_risk, remaining_risk)
        allocated_margin = min(
            snapshot.available_margin,
            snapshot.net_liquidation_equity * allocation,
        )
        return affordable_lots(
            risk_budget=risk_allowance,
            max_loss_per_lot=_decimal(max_loss_per_lot, "max_loss_per_lot"),
            estimated_cost_per_lot=_decimal(estimated_cost_per_lot, "estimated_cost_per_lot"),
            available_margin=allocated_margin,
            margin_per_lot=_decimal(margin_per_lot, "margin_per_lot"),
            max_lots=max_lots,
        )

    def report(self, *, now: datetime | None = None) -> dict[str, str] | None:
        snapshot = self.snapshot(now=now)
        if snapshot is None:
            return None
        events = self._events()
        opened = next(e for e in events if e["event_type"] == "ACCOUNT_OPENED")
        deposits = sum(
            (
                _decimal(e["payload"]["amount"], "amount")
                for e in events
                if e["event_type"] == "ACCOUNT_CASH_FLOW"
                and e["payload"]["direction"] == "DEPOSIT"
            ),
            Decimal("0"),
        )
        withdrawals = sum(
            (
                _decimal(e["payload"]["amount"], "amount")
                for e in events
                if e["event_type"] == "ACCOUNT_CASH_FLOW"
                and e["payload"]["direction"] == "WITHDRAWAL"
            ),
            Decimal("0"),
        )
        pnl = sum(
            (
                _decimal(e["payload"]["realized_pnl"], "realized_pnl")
                - _decimal(e["payload"]["fees"], "fees")
                for e in events if e["event_type"] == "ACCOUNT_TRADE_RESULT"
            ),
            Decimal("0"),
        )
        fees = sum((
            _decimal(e["payload"]["fees"], "fees")
            for e in events if e["event_type"] == "ACCOUNT_TRADE_RESULT"
        ), Decimal("0"))
        cash_balance = (
            _decimal(opened["payload"]["amount"], "opening_equity")
            + deposits - withdrawals + pnl
        )
        latest = next(e["payload"] for e in reversed(events)
                      if e["event_type"] == "ACCOUNT_RECONCILED_MARK")
        return {
            "currency": self.currency,
            "opening_equity": str(opened["payload"]["amount"]),
            "deposits": str(deposits),
            "withdrawals": str(withdrawals),
            "realized_net_pnl": str(pnl),
            "fees_and_taxes": str(fees),
            "cash_balance": str(latest.get("actual_cash") or cash_balance),
            "unrealized_pnl": str(snapshot.daily_unrealized_pnl),
            "net_liquidation_equity": str(snapshot.net_liquidation_equity),
            "available_margin": str(snapshot.available_margin),
            "open_max_loss": str(snapshot.open_max_loss),
            "peak_equity": str(snapshot.peak_equity),
            "weekly_drawdown_pct": str(snapshot.weekly_drawdown_pct * 100),
            "monthly_drawdown_pct": str(snapshot.monthly_drawdown_pct * 100),
            "open_positions": str(snapshot.open_positions),
            "entries_today": str(snapshot.entries_today),
        }
