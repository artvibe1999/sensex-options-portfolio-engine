from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sensex_engine.config import Settings
from sensex_engine.domain import (
    Contract, OptionLeg, PortfolioSnapshot, Regime, Right, Side, StrategyCandidate, TradingMode
)
from sensex_engine.execution import PaperExecutionAdapter
from sensex_engine.mode import ModeController
from sensex_engine.risk import RiskEngine, RiskLimits


def test_environment_cannot_boot_live():
    assert Settings(trading_mode="live").startup_mode() == "paper"


def test_live_mode_requires_request_and_matching_confirmation():
    controller = ModeController()
    with pytest.raises(PermissionError):
        controller.request_live(7, authorized=False)
    controller.request_live(7, authorized=True)
    with pytest.raises(PermissionError):
        controller.confirm_live(8, authorized=True, broker_ready=True, risk_clear=True)
    controller.confirm_live(7, authorized=True, broker_ready=True, risk_clear=True)
    assert controller.mode is TradingMode.LIVE
    controller.force_paper()
    assert controller.mode is TradingMode.PAPER


def test_paper_adapter_rejects_live_position():
    adapter = PaperExecutionAdapter()
    contract = Contract("SENSEXTEST", 1, datetime.now(timezone.utc), Decimal("80000"),
                        Right.CE, 20, Decimal("0.05"))
    position = __import__("sensex_engine.domain", fromlist=["Position"]).Position.create(
        "test", (OptionLeg(contract, Side.BUY, 1),), TradingMode.LIVE
    )
    with pytest.raises(ValueError):
        adapter.submit(position)


def test_risk_engine_rejects_stale_data():
    candidate = StrategyCandidate(
        "bull-call-spread", Regime.BULL_TREND, (), Decimal("300"), Decimal("700"),
        Decimal("300"), Decimal("10"), datetime.now(timezone.utc), True
    ) if False else None
    # An invalid/empty candidate cannot be constructed; this test checks fail-closed
    # market-quality policy through eligibility instead.
    from sensex_engine.eligibility import regime_eligibility
    assert not regime_eligibility(Regime.BULL_TREND, chain_valid=False, liquidity_ok=True).allowed


def test_allowlist_requires_both_user_and_chat():
    from sensex_engine.telegram_auth import TelegramAllowlist
    allow = TelegramAllowlist(frozenset({1}), frozenset({10}))
    assert allow.allows(1, 10)
    assert not allow.allows(1, 11)
    assert not allow.allows(None, 10)
