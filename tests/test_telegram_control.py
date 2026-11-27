import pytest

from sensex_engine.domain import TradingMode
from sensex_engine.mode import ModeController
from sensex_engine.order_lifecycle import OrderLifecycle
from sensex_engine.persistence import SqliteStore
from sensex_engine.portfolio_queries import PortfolioQueryService
from sensex_engine.position_lifecycle import PositionLifecycle
from sensex_engine.telegram_auth import TelegramAllowlist
from sensex_engine.telegram_control import OperationsController


def make_ops():
    return OperationsController(
        TelegramAllowlist(frozenset({11}), frozenset({22})),
        ModeController(),
    )


def test_unauthorized_telegram_command_is_rejected():
    with pytest.raises(PermissionError):
        make_ops().handle("/paper", user_id=11, chat_id=99)


def test_paper_command_forces_paper_and_pauses_entries():
    ops = make_ops()
    ops.paused = False
    response = ops.handle("/paper", user_id=11, chat_id=22)
    assert ops.mode.mode is TradingMode.PAPER
    assert ops.paused
    assert "Paper mode" in response


def test_live_requires_request_then_unpaused_confirmation_and_checks():
    ops = make_ops()
    ops.handle("/live", user_id=11, chat_id=22)
    with pytest.raises(RuntimeError):
        ops.handle(
            "/confirm_live", user_id=11, chat_id=22,
            broker_ready=True, risk_clear=True,
        )
    ops.paused = False
    with pytest.raises(RuntimeError, match="Startup recovery"):
        ops.handle(
            "/confirm_live", user_id=11, chat_id=22,
            broker_ready=True, risk_clear=True,
        )
    ops.startup_recovery_complete = True
    with pytest.raises(RuntimeError):
        ops.handle(
            "/confirm_live", user_id=11, chat_id=22,
            broker_ready=False, risk_clear=True,
        )
    ops.handle(
        "/confirm_live", user_id=11, chat_id=22,
        broker_ready=True, risk_clear=True,
    )
    assert ops.mode.mode is TradingMode.LIVE


def test_resume_fails_closed_without_broker_and_risk_checks():
    ops = make_ops()
    with pytest.raises(RuntimeError, match="startup recovery"):
        ops.handle(
            "/resume", user_id=11, chat_id=22,
            broker_ready=True, risk_clear=True,
        )
    ops.startup_recovery_complete = True
    with pytest.raises(RuntimeError):
        ops.handle("/resume", user_id=11, chat_id=22)



def test_authorized_read_only_positions_and_orders_commands_use_internal_ledgers(tmp_path):
    store = SqliteStore(tmp_path / "telegram-portfolio.sqlite3")
    queries = PortfolioQueryService(
        positions=PositionLifecycle(store),
        orders=OrderLifecycle(store),
    )
    ops = make_ops()
    ops.portfolio_queries = queries
    positions = ops.handle("/positions", user_id=11, chat_id=22)
    orders = ops.handle("/orders", user_id=11, chat_id=22)
    assert "Internal position ledger" in positions
    assert "Internal order ledger" in orders
    assert "not broker-confirmed" in positions
    assert "not broker-confirmed" in orders


def test_account_queries_remain_disabled_without_authoritative_services():
    with pytest.raises(NotImplementedError, match="authoritative account"):
        make_ops().handle("/pnl", user_id=11, chat_id=22)
