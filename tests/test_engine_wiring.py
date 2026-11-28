from types import SimpleNamespace

import pytest
from engine_fakes import FAR, NEAR, NOW, FakeKite, chain

from sensex_engine import strategies as S


def _ctx(state):
    return S.MarketContext(state=state, near=chain(NEAR), far=chain(FAR), oi={}, now=NOW)


class FakePositions:
    def __init__(self, legs):
        self.legs = legs

    def snapshot(self, position_id):
        return {"position_id": position_id, "strategy_id": S.LEG_IN_CALL, "legs": self.legs}


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
