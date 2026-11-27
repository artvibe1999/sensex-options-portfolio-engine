"""Reconcile the live ledger against fills, quotes, and an independent account feed.

The feed is an operator-controlled normalized broker statement, not a Kite margin
response. Missing final charges, absent account data or disagreement block entries.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from .accounting import PortfolioAccountingService, _decimal
from .domain import PortfolioSnapshot
from .filled_risk import filled_position_risk
from .market_data import validate_quote
from .reconciliation import BrokerReconciliationService

IST = ZoneInfo("Asia/Kolkata")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Account source timestamps must be timezone-aware")
    return result


class StatementFeed:
    """Read a fresh, independently supplied and trusted normalized statement."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def read(self, now: datetime) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data["source"] != "broker_account_statement" or data["currency"] != "INR":
            raise ValueError("Independent INR broker account statement required")
        as_of = timestamp(data["as_of"])
        if not timedelta(0) <= now - as_of <= timedelta(minutes=2):
            raise ValueError("Broker account statement is stale or future-dated")
        if not data["statement_id"] or not isinstance(data["charges_by_fill"], dict):
            raise ValueError("Statement identity and final charges required")
        return data


class LiveAccountingRefresh:
    """Read-only repeatable reconciliation; never sends orders."""

    def __init__(self, *, account: PortfolioAccountingService, broker: Any,
                 positions: Any, orders: Any, feed: StatementFeed) -> None:
        self.account = account
        self.broker = broker
        self.positions = positions
        self.orders = orders
        self.feed = feed
        self._lock = Lock()

    def refresh(self, *, now: datetime | None = None) -> PortfolioSnapshot:
        now = now or datetime.now(UTC)
        self.account.invalidate_mark()
        data = self.feed.read(now)
        as_of = timestamp(data["as_of"])
        opening = _decimal(data["opening_equity"], "opening equity")
        if opening <= 0:
            raise ValueError("Opening equity must be positive")
        expected_equity = opening
        flows = []
        flow_ids = set()
        results = []
        for flow in data["cash_flows"]:
            occurred = timestamp(flow["occurred_at"])
            if occurred > as_of:
                raise ValueError("Cash flow is newer than the statement")
            amount = _decimal(flow["amount"], "cash flow")
            if (amount <= 0 or flow["direction"] not in {"DEPOSIT", "WITHDRAWAL"}
                    or not flow["id"] or flow["id"] in flow_ids):
                raise ValueError("Invalid or duplicate cash flow")
            flow_ids.add(flow["id"])
            expected_equity += amount if flow["direction"] == "DEPOSIT" else -amount
            flows.append((flow, amount, occurred))
        contracts = self.broker.instruments()
        report = BrokerReconciliationService().reconcile_store(
            self.broker, contracts=contracts, position_lifecycle=self.positions,
            order_lifecycle=self.orders,
        )
        if not report.reconciled:
            raise ValueError("Broker exposure or orders are not reconciled")
        charges = data["charges_by_fill"]
        unrealized = Decimal("0")
        seen = set()
        entries = 0
        active = []
        for pid in self.account.store.aggregate_ids_for_event("POSITION_CREATED"):
            position = self.positions.snapshot(pid)
            if position is None or position["mode"] != "live":
                continue
            events = self.account.store.events_for(pid)
            legs = {leg["leg_index"]: leg for leg in position["legs"]}
            basis: dict[int, tuple[int, Decimal]] = {}
            first_open = None
            for event in events:
                if event["event_type"] != "POSITION_LEG_FILL_RECORDED":
                    continue
                fill = event["payload"]
                fid = fill["fill_id"]
                at = timestamp(fill["filled_at"])
                if fid in seen or fid not in charges or at > as_of:
                    raise ValueError("Missing final charge, duplicate fill or newer fill")
                seen.add(fid)
                index = fill["leg_index"]
                units = fill["quantity"]
                price = _decimal(fill["price"], "fill price")
                qty, avg = basis.get(index, (0, Decimal("0")))
                pnl = Decimal("0")
                if fill["phase"] == "OPEN":
                    first_open = min(first_open, at) if first_open else at
                    avg = (avg * qty + price * units) / (qty + units)
                    qty += units
                else:
                    if units > qty:
                        raise ValueError("Closing fill exceeds opening quantity")
                    direction = 1 if legs[index]["side"] == "BUY" else -1
                    pnl = (price - avg) * units * direction
                    qty -= units
                basis[index] = qty, avg
                charge = _decimal(charges[fid], "final charges")
                if charge < 0:
                    raise ValueError("Final charges cannot be negative")
                expected_equity += pnl - charge
                results.append((fid, pnl, charge, at))
            if first_open and first_open.astimezone(IST).date() == now.astimezone(IST).date():
                entries += 1
            if any(qty for qty, _ in basis.values()):
                active.append(position)
                tokens = [
                    legs[index]["instrument_token"]
                    for index, (qty, _) in basis.items() if qty
                ]
                quotes = self.broker.quotes(tokens)
                for index, (qty, avg) in basis.items():
                    if not qty:
                        continue
                    leg = legs[index]
                    quote = quotes[leg["instrument_token"]]
                    if not validate_quote(quote, now=now).valid:
                        raise ValueError("Open exposure has no valid liquidation quote")
                    long = leg["side"] == "BUY"
                    mark = quote.bid if long else quote.ask
                    unrealized += (mark - avg) * qty * (1 if long else -1)
        if seen != set(charges):
            raise ValueError("Statement contains charges without matching durable fills")
        loss, count = filled_position_risk(active, contracts, mode="live")
        margin = self.broker.margins("equity")
        if margin.get("enabled") is not True:
            raise ValueError("Broker equity segment is disabled")
        equity = _decimal(data["net_liquidation_equity"], "statement equity")
        cash = _decimal(data["cash_balance"], "statement cash")
        if abs(expected_equity + unrealized - equity) > Decimal("1.00"):
            raise ValueError("Independent statement disagrees with reconstructed equity")
        # Do not append unverified financial facts until the full projection balances.
        existing = self.account._events()
        expected_keys = {"account:statement-opening"}
        expected_keys.update(f"account:flow:{flow[\'id\']}" for flow, _, _ in flows)
        expected_keys.update(f"account:fill:{fid}" for fid, _, _, _ in results)
        if any(e["idempotency_key"] not in expected_keys for e in existing
               if e["event_type"] in {
                   "ACCOUNT_OPENED", "ACCOUNT_CASH_FLOW", "ACCOUNT_TRADE_RESULT",
               }):
            raise ValueError("Statement omits previously recorded ledger facts")
        self.account.set_opening_equity(opening, idempotency_key="statement-opening")
        for flow, amount, occurred in flows:
            self.account.record_cash_flow(
                amount, direction=flow["direction"], idempotency_key=f"flow:{flow['id']}",
                occurred_at=occurred, reference=flow["id"],
            )
        for fid, pnl, charge, at in results:
            self.account.record_trade_result(
                realized_pnl=pnl, fees=charge, idempotency_key=f"fill:{fid}",
                occurred_at=at, broker_trade_ids=(fid,),
            )
        if abs(self.account.ledger_equity(unrealized) - equity) > Decimal("1.00"):
            raise ValueError("Persisted ledger disagrees with independently verified equity")
        self.account.record_reconciled_mark(
            broker_net_equity=equity, unrealized_pnl=unrealized,
            available_margin=_decimal(margin["net"], "available margin"),
            open_max_loss=loss, open_positions=count, entries_today=entries,
            reconciliation_id=f"{data['statement_id']}:{now.isoformat()}",
            reconciliation_report=report, marked_at=as_of, actual_cash=cash,
        )
        snapshot = self.account.snapshot(now=now)
        if snapshot is None:
            raise ValueError("Statement equity disagrees with reconstructed ledger")
        return snapshot

    def snapshot(self) -> PortfolioSnapshot | None:
        with self._lock:
            try:
                return self.refresh()
            except Exception:
                self.account.invalidate_mark()
                return None
