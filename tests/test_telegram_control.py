import pytest

from sensex_engine.domain import TradingMode
from sensex_engine.mode import ModeController
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
    with pytest.raises(RuntimeError):
        ops.handle("/resume", user_id=11, chat_id=22)
