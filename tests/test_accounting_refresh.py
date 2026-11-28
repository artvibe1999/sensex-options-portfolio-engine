import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sensex_engine.accounting import PortfolioAccountingService
from sensex_engine.accounting_refresh import LiveAccountingRefresh, StatementFeed
from sensex_engine.domain import Contract, Right
from sensex_engine.market_data import Quote
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


def test_open_filled_risk_and_unrealized_pnl_from_executable_marks(tmp_path):
    store = SqliteStore(tmp_path / "open-account.sqlite3")
    account = PortfolioAccountingService(store=store)
    positions = PositionLifecycle(store)
    now = datetime.now(UTC)
    expiry = now + timedelta(days=5)
    contracts = [
        Contract("A", 1, expiry, Decimal("80000"), Right.CE, 20, Decimal(".05")),
        Contract("B", 2, expiry, Decimal("80100"), Right.CE, 20, Decimal(".05")),
    ]
    positions.create(
        position_id="p", strategy_id="VERTICAL", mode="live", idempotency_key="p",
        legs=[
            dict(leg_index=0, instrument_token=1, tradingsymbol="A",
                 side="BUY", quantity=20),
            dict(leg_index=1, instrument_token=2, tradingsymbol="B",
                 side="SELL", quantity=20),
        ],
    )
    for index, price, fid in [(0, "100", "long"), (1, "60", "short")]:
        positions.record_leg_fill(
            position_id="p", leg_index=index, phase="OPEN", fill_id=fid,
            quantity=20, price=Decimal(price), filled_at=now.isoformat(),
        )

    class OpenBroker(FlatBroker):
        def instruments(self):
            return contracts

        def positions(self):
            return {"net": [
                {"exchange": "BFO", "instrument_token": 1, "quantity": 20},
                {"exchange": "BFO", "instrument_token": 2, "quantity": -20},
            ]}

        def quotes(self, tokens):
            return {
                1: Quote(1, now, Decimal("110"), Decimal("110"), Decimal("111"), 50, 50, 500),
                2: Quote(2, now, Decimal("65"), Decimal("64"), Decimal("65"), 50, 50, 500),
            }

    source = tmp_path / "statement.json"
    source.write_text(json.dumps({
        "source": "broker_account_statement", "currency": "INR",
        "as_of": now.isoformat(), "statement_id": "open",
        "opening_equity": "50000", "cash_flows": [],
        "charges_by_fill": {"long": "10", "short": "15"},
        "net_liquidation_equity": "50075", "cash_balance": "49175",
    }))
    refresh = LiveAccountingRefresh(
        account=account, broker=OpenBroker(), positions=positions,
        orders=OrderLifecycle(store), feed=StatementFeed(str(source)),
    )
    snap = refresh.refresh(now=now)
    assert snap.net_liquidation_equity == Decimal("50075")
    assert snap.daily_unrealized_pnl == Decimal("100")
    assert snap.daily_realized_pnl == Decimal("-25")
    assert snap.open_max_loss == Decimal("900")
    assert snap.strategy_open_risk == (("VERTICAL", Decimal("900")),)
    assert snap.open_positions == 1
