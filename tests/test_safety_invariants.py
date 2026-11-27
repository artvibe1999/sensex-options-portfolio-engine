from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sensex_engine.config import Settings
from sensex_engine.domain import (
    Contract,
    OptionLeg,
    PortfolioSnapshot,
    Position,
    Regime,
    Right,
    Side,
    StrategyCandidate,
    TradingMode,
)
from sensex_engine.eligibility import regime_eligibility
from sensex_engine.execution import PaperExecutionAdapter
from sensex_engine.mode import ModeController
from sensex_engine.risk import RiskEngine


def make_candidate() -> StrategyCandidate:
    contract = Contract(
        "SENSEXTEST",
        1,
        datetime.now(UTC),
        Decimal("80000"),
        Right.CE,
        20,
        Decimal("0.05"),
    )
    return StrategyCandidate(
        "bull-call-spread",
        Regime.BULL_TREND,
        (OptionLeg(contract, Side.BUY, 1),),
        Decimal("300"),
        Decimal("700"),
        Decimal("300"),
        Decimal("10"),
        datetime.now(UTC),
        True,
    )


def make_portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        net_liquidation_equity=Decimal("50000"),
        available_margin=Decimal("20000"),
        open_max_loss=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
    )


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
    position = Position.create("test", make_candidate().legs, TradingMode.LIVE)
    with pytest.raises(ValueError):
        adapter.submit(position)


def test_risk_engine_rejects_stale_data():
    decision = RiskEngine().evaluate(
        make_candidate(),
        make_portfolio(),
        estimated_margin=Decimal("1000"),
        data_is_fresh=False,
        contracts_are_current=True,
        broker_reconciled=True,
    )
    assert not decision.approved
    assert decision.reason == "Market data is stale or unavailable"


def test_risk_engine_approves_candidate_inside_limits():
    decision = RiskEngine().evaluate(
        make_candidate(),
        make_portfolio(),
        estimated_margin=Decimal("1000"),
        data_is_fresh=True,
        contracts_are_current=True,
        broker_reconciled=True,
    )
    assert decision.approved


def test_uncertain_regime_is_no_trade():
    assert not regime_eligibility(
        Regime.TRANSITION,
        chain_valid=True,
        liquidity_ok=True,
    ).allowed


def test_allowlist_requires_both_user_and_chat():
    from sensex_engine.telegram_auth import TelegramAllowlist

    allow = TelegramAllowlist(frozenset({1}), frozenset({10}))
    assert allow.allows(1, 10)
    assert not allow.allows(1, 11)
    assert not allow.allows(None, 10)

def test_risk_engine_reduces_trade_budget_after_equity_drawdown():
    portfolio = make_portfolio()
    portfolio = PortfolioSnapshot(
        net_liquidation_equity=Decimal("48000"),
        available_margin=portfolio.available_margin,
        open_max_loss=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
        peak_equity=Decimal("50000"),
    )
    decision = RiskEngine().evaluate(
        make_candidate(),
        portfolio,
        estimated_margin=Decimal("1000"),
        data_is_fresh=True,
        contracts_are_current=True,
        broker_reconciled=True,
    )
    assert not decision.approved
    assert "adaptive per-trade" in decision.reason


def test_risk_engine_rejects_unvalidated_volatility_expansion_strategy():
    from dataclasses import replace

    candidate = replace(
        make_candidate(),
        regime=Regime.VOLATILITY_EXPANSION,
    )
    decision = RiskEngine().evaluate(
        candidate,
        make_portfolio(),
        estimated_margin=Decimal("1000"),
        data_is_fresh=True,
        contracts_are_current=True,
        broker_reconciled=True,
    )
    assert not decision.approved
    assert "separately validated" in decision.reason
