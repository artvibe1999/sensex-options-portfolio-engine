import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sensex_engine.accounting import PortfolioAccountingService
from sensex_engine.accounting_refresh import LiveAccountingRefresh, StatementFeed
from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


class FlatBroker:
    def instruments(self):
        return []

    def positions(self):
        return {"net": []}

    def orders(self):
        return []

    def is_authenticated(self):
        return True

    def margins(self, segment):
        return {"enabled": True, "net": "30000"}


def test_refresh_books_closed_fills_once_and_invalidates_stale_source(tmp_path):
    store = SqliteStore(tmp_path / "account.sqlite3")
    account = PortfolioAccountingService(store=store)
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    now = datetime.now(UTC)
    positions.create(
        position_id="p", strategy_id="test", mode="live",
        legs=[dict(leg_index=0, instrument_token=1, tradingsymbol="A",
                   side="BUY", quantity=20)], idempotency_key="p",
    )
    positions.record_leg_fill(
        position_id="p", leg_index=0, phase="OPEN", fill_id="open",
        quantity=20, price=Decimal("100"), filled_at=now.isoformat(),
    )
    positions.transition(position_id="p", status="CLOSING", update_id="close")
    positions.record_leg_fill(
        position_id="p", leg_index=0, phase="CLOSE", fill_id="close",
        quantity=20, price=Decimal("110"), filled_at=now.isoformat(),
    )
    positions.finalize_flat(position_id="p", update_id="done")
    source = tmp_path / "statement.json"
    data = {
        "source": "broker_account_statement", "currency": "INR",
        "as_of": now.isoformat(), "statement_id": "statement",
        "opening_equity": "50000", "cash_flows": [],
        "charges_by_fill": {"open": "10", "close": "15"},
        "net_liquidation_equity": "50175", "cash_balance": "50175",
    }
    source.write_text(json.dumps(data))
    refresh = LiveAccountingRefresh(
        account=account, broker=FlatBroker(), positions=positions,
        orders=orders, feed=StatementFeed(str(source)),
    )
    snap = refresh.refresh(now=now)
    assert snap.net_liquidation_equity == Decimal("50175")
    assert snap.daily_realized_pnl == Decimal("175")
    assert snap.entries_today == 1
    assert refresh.refresh(now=now + timedelta(seconds=1)).daily_realized_pnl == Decimal("175")
    assert account.report(now=now)["cash_balance"] == "50175"
    with pytest.raises(ValueError, match="stale"):
        refresh.refresh(now=now + timedelta(minutes=3))
    assert account.snapshot(now=now) is None


def test_statement_rejects_margin_as_equity_source(tmp_path):
    source = tmp_path / "statement.json"
    source.write_text(json.dumps({"source": "kite_margins", "currency": "INR"}))
    with pytest.raises(ValueError, match="Independent"):
        StatementFeed(str(source)).read(datetime.now(UTC))
