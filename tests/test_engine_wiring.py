from decimal import Decimal
from types import SimpleNamespace

import pytest
from engine_fakes import FAR, NEAR, NOW, FakeKite, bullish_state, chain, make_state

from sensex_engine import strategies as S
from sensex_engine.capital_policy import risk_policy
from sensex_engine.engine import LiveBridge, LiveEntryError


def _ctx(state):
    return S.MarketContext(state=state, near=chain(NEAR), far=chain(FAR), oi={}, now=NOW)


class FakePositions:
    def __init__(self, legs):
        self.legs = legs

    def snapshot(self, position_id):
        return {"position_id": position_id, "strategy_id": S.BULL_CALL, "legs": self.legs}


class FakeCoordinator:
    def __init__(self, completed=True):
        self.completed = completed
        self.sized = None

    def execute_sized(self, candidate, *, max_lots, allocation_pct):
        self.sized = (candidate, max_lots, allocation_pct)
        return SimpleNamespace(
            position_id="11111111-1111-1111-1111-111111111111",
            completed=self.completed,
            reason="r",
            residual_exposure=(),
        )


class FakeAdapter:
    def __init__(self, quotes):
        self._quotes = quotes

    def quotes(self, tokens):
        return {t: self._quotes[t] for t in tokens if t in self._quotes}


def test_live_bridge_maps_confirmed_fills_and_uses_tier_limits():
    proposal, why, _ = S.build_bull_call(_ctx(bullish_state()), S.StrategyConfig(), Decimal("9000"))
    assert proposal is not None, why
    legs = [
        {
            "instrument_token": a.contract.instrument_token,
            "quantity": 40,
            "open_average_price": str(p),
            "side": side.value,
        }
        for (a, side), p in zip(proposal.legs, proposal.prices, strict=True)
    ]
    coordinator = FakeCoordinator()
    sizing = SimpleNamespace(risk=None)
    quotes = {a.contract.instrument_token: a.quote for a, _ in proposal.legs}
    bridge = LiveBridge(
        coordinator=coordinator,
        positions=FakePositions(legs),
        sizing=sizing,
        adapter=FakeAdapter(quotes),
        snapshot_provider=lambda: None,
    )
    policy = risk_policy(equity=Decimal("500000"), peak_equity=Decimal("500000"))
    pid, lots, fills = bridge.open(proposal, lots=2, policy=policy, allocation_pct=Decimal("0.7"))
    assert lots == 2 and set(fills) == set(quotes)
    candidate, max_lots, _ = coordinator.sized
    assert max_lots == 2 and all(leg.lots == 1 for leg in candidate.legs)
    assert sizing.risk.limits.risk_per_trade_pct == policy.limits.risk_per_trade_pct
    bridge.coordinator = FakeCoordinator(completed=False)
    with pytest.raises(LiveEntryError):
        bridge.open(proposal, lots=1, policy=policy, allocation_pct=Decimal("0.7"))


def test_live_bridge_refuses_multi_expiry_structures():
    proposal, _, _ = S.build_calendar(_ctx(make_state()), S.StrategyConfig(), Decimal("20000"))
    bridge = LiveBridge(
        coordinator=FakeCoordinator(),
        positions=FakePositions([]),
        sizing=SimpleNamespace(risk=None),
        adapter=FakeAdapter({}),
        snapshot_provider=lambda: None,
    )
    policy = risk_policy(equity=Decimal("500000"), peak_equity=Decimal("500000"))
    with pytest.raises(LiveEntryError, match="paper only"):
        bridge.open(proposal, lots=1, policy=policy, allocation_pct=Decimal("0.7"))


def _settings(tmp_path, **kw):
    from sensex_engine.config import Settings

    base = dict(
        kite_api_key="key",
        kite_access_token="tok",
        engine_db_path=str(tmp_path / "e.db"),
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        heartbeat_path=str(tmp_path / "hb"),
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _restore_reconciliation_scope():
    from sensex_engine import reconciliation

    yield
    reconciliation.configure_scope(managed=False)


def test_build_headless_and_with_telegram(tmp_path, monkeypatch):
    kiteconnect = pytest.importorskip("kiteconnect")  # optional [broker] extra
    pytest.importorskip("telegram")  # optional [telegram] extra

    from sensex_engine import app

    monkeypatch.setattr(kiteconnect, "KiteConnect", lambda api_key: FakeKite())
    engine, application, adapter = app.build(_settings(tmp_path, reconcile_scope="managed"))
    assert application is None and engine.mode == "paper" and engine.live is None
    from sensex_engine.reconciliation import managed_scope

    assert managed_scope()
    assert isinstance(adapter, app.CachedKiteAdapter)
    first = adapter.instruments()
    assert adapter.instruments() == first and adapter.futures

    engine, application, _ = app.build(
        _settings(
            tmp_path / "t",
            telegram_bot_token="123456:ABCDEF",
            telegram_allowed_user_ids="1",
            telegram_allowed_chat_ids="2",
            kite_static_ip_confirmed=True,
        )
    )
    ops = application.bot_data["operations_controller"]
    assert ops.engine is engine and engine.operations is ops
    assert engine.live is not None and ops.paused and engine.mode == "paper"
    with pytest.raises(ValueError):
        app._engine_config(_settings(tmp_path, enabled_strategies="NOPE"))
