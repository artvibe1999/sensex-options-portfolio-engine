from datetime import UTC, datetime
from decimal import Decimal

from sensex_engine.accounting import PortfolioAccountingService
from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.portfolio_queries import PortfolioQueryService
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.reconciliation import ReconciliationReport


def make_queries(tmp_path, *, max_items=12, max_message_chars=3500):
    store = SqliteStore(tmp_path / "portfolio-queries.sqlite3")
    positions = PositionLifecycle(store)
    orders = OrderLifecycle(store)
    return PortfolioQueryService(
        positions=positions, orders=orders,
        max_items=max_items, max_message_chars=max_message_chars,
    ), positions, orders


def test_empty_portfolio_queries_are_explicitly_internal(tmp_path):
    queries, _, _ = make_queries(tmp_path)
    assert "no active positions" in queries.positions_text()
    assert "not a broker reconciliation" in queries.positions_text()
    assert "no order intents" in queries.orders_text()
    assert "not a broker order-book" in queries.orders_text()


def test_positions_query_reports_actual_open_fills_and_status(tmp_path):
    queries, positions, _ = make_queries(tmp_path)
    positions.create(
        position_id="pos-1", strategy_id="bull-call", mode="paper",
        legs=[{
            "leg_index": 0, "instrument_token": 123,
            "tradingsymbol": "SENSEX80000CE", "side": "BUY", "quantity": 10,
        }],
        idempotency_key="create-pos-1",
    )
    result = queries.positions_text()
    assert "Internal position ledger" in result
    assert "pos-1 | bull-call | PENDING" in result
    assert "remaining=0" in result


def test_orders_query_reports_fill_progress_without_broker_claim(tmp_path):
    queries, _, orders = make_queries(tmp_path)
    orders.create_intent(
        order_id="order-1", position_id="pos-1", leg_index=0,
        instrument_token=123, tradingsymbol="SENSEX80000CE",
        side="BUY", quantity=10, order_type="LIMIT", product="NRML",
        mode="paper", idempotency_key="create-order-1",
    )
    result = queries.orders_text()
    assert "Internal order ledger" in result
    assert "order-1 | CREATED | BUY SENSEX80000CE" in result
    assert "filled=0/10 remaining=10" in result
    assert "not broker-confirmed" in result


def test_query_output_is_bounded_and_sanitizes_line_breaks(tmp_path):
    queries, positions, _ = make_queries(tmp_path, max_message_chars=256)
    for index in range(4):
        positions.create(
            position_id=f"position-{index}", strategy_id="strategy\nspoofed",
            mode="paper",
            legs=[{
                "leg_index": 0, "instrument_token": 100 + index,
                "tradingsymbol": f"SENSEX{80000 + index}CE",
                "side": "BUY", "quantity": 1,
            }],
            idempotency_key=f"create-{index}",
        )
    result = queries.positions_text()
    assert len(result) <= 256
    assert "strategy spoofed" in result
    assert "spoofed\n" not in result


def test_invalid_output_limits_are_rejected(tmp_path):
    _, positions, orders = make_queries(tmp_path)
    try:
        PortfolioQueryService(positions=positions, orders=orders, max_items=0)
    except ValueError as exc:
        assert "limits" in str(exc)
    else:
        raise AssertionError("invalid output limits must fail")


def test_accounting_reports_are_unavailable_until_fresh_reconciled_mark(tmp_path):
    store = SqliteStore(tmp_path / "accounting-queries.sqlite3")
    accounting = PortfolioAccountingService(store=store)
    queries = PortfolioQueryService(
        positions=PositionLifecycle(store), orders=OrderLifecycle(store),
        accounting=accounting,
    )
    assert "LIVE readiness remains blocked" in queries.pnl_text()
    accounting.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    assert "LIVE readiness remains blocked" in queries.capital_text()


def test_accounting_telegram_views_render_reconciled_values(tmp_path):
    store = SqliteStore(tmp_path / "accounting-queries-ready.sqlite3")
    accounting = PortfolioAccountingService(store=store)
    now = datetime.now(UTC)
    accounting.set_opening_equity(Decimal("50000"), idempotency_key="opening")
    accounting.record_trade_result(
        realized_pnl=Decimal("500"), fees=Decimal("50"),
        idempotency_key="trade", occurred_at=now,
    )
    accounting.record_reconciled_mark(
        broker_net_equity=Decimal("50450"), unrealized_pnl=Decimal("0"),
        available_margin=Decimal("30000"), open_max_loss=Decimal("0"),
        open_positions=0, entries_today=1, reconciliation_id="verified",
        reconciliation_report=ReconciliationReport(True, (), (), (), ()), marked_at=now,
    )
    queries = PortfolioQueryService(
        positions=PositionLifecycle(store), orders=OrderLifecycle(store),
        accounting=accounting,
    )
    assert "Realised net P&L: 450" in queries.pnl_text()
    assert "Fees and taxes: 50" in queries.pnl_text()
    assert "Cash balance: 50450" in queries.capital_text()
    assert "Net liquidation equity: 50450" in queries.capital_text()
    assert "Open defined max loss: 0" in queries.risk_text()
