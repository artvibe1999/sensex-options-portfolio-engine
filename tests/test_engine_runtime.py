import json
from types import SimpleNamespace

import pytest

from sensex_engine import reconciliation
from sensex_engine.kite_session import AccessTokenProvider, SharedKiteClient
from sensex_engine.mode import ModeController
from sensex_engine.reconciliation import reconcile_snapshots
from sensex_engine.telegram_auth import TelegramAllowlist
from sensex_engine.telegram_control import OperationsController


@pytest.fixture
def managed_scope():
    reconciliation.configure_scope(managed=True)
    yield
    reconciliation.configure_scope(managed=False)


def _contract(token, symbol):
    return SimpleNamespace(instrument_token=token, tradingsymbol=symbol, lot_size=20)


def _position(token, symbol, qty_lots, side="BUY"):
    leg = SimpleNamespace(contract=_contract(token, symbol), side=side, lots=qty_lots)
    return SimpleNamespace(position_id="p", status="OPEN", legs=(leg,))


CONTRACTS = [_contract(1, "SENSEXA"), _contract(2, "SENSEXB")]


def test_strict_scope_blocks_foreign_sensex_exposure():
    report = reconcile_snapshots(
        contracts=CONTRACTS,
        internal_positions=[],
        broker_positions={"net": [{"exchange": "BFO", "instrument_token": 2, "quantity": 20}]},
        broker_orders=[],
    )
    assert not report.reconciled


def test_managed_scope_ignores_other_bot_but_not_overlap(managed_scope):
    foreign = {
        "net": [
            {"exchange": "BFO", "instrument_token": 2, "quantity": -20, "tradingsymbol": "SENSEXB"},
            {"exchange": "BFO", "tradingsymbol": "SENSEXZZ", "quantity": 40},
        ]
    }
    other_order = [
        {
            "status": "OPEN",
            "order_id": "9",
            "tag": "sx12",
            "instrument_token": 2,
            "tradingsymbol": "SENSEXB",
        }
    ]
    ok = reconcile_snapshots(
        contracts=CONTRACTS,
        internal_positions=[],
        broker_positions=foreign,
        broker_orders=other_order,
    )
    assert ok.reconciled, ok.mismatches
    # The engine holds token 2 too: the other bot's quantity now overlaps -> mismatch.
    clash = reconcile_snapshots(
        contracts=CONTRACTS,
        internal_positions=[_position(2, "SENSEXB", 1)],
        broker_positions=foreign,
        broker_orders=[],
    )
    assert not clash.reconciled
    # An active order tagged by this engine always counts.
    ours = reconcile_snapshots(
        contracts=CONTRACTS,
        internal_positions=[],
        broker_positions={"net": []},
        broker_orders=[
            {"status": "OPEN", "order_id": "1", "tag": "PFabc", "tradingsymbol": "SENSEXA"}
        ],
    )
    assert not ours.reconciled and ours.pending_order_ids == ("1",)


def test_engine_tag_prefix_is_distinct_from_other_bots():
    from sensex_engine.kite_execution import KiteOrderExecutionAdapter

    tag = KiteOrderExecutionAdapter.tag_for_client_key("abc")
    assert tag.startswith("PF") and len(tag) == 20
    assert not tag.lower().startswith(("sx", "st"))


def test_token_provider_reads_shared_file_and_falls_back(tmp_path):
    path = tmp_path / "token.json"
    provider = AccessTokenProvider(token_file=str(path), fallback_token="envtok")
    assert provider.current().source == "env"
    path.write_text(
        json.dumps({"access_token": "filetok", "saved_at": "2026-11-24T08:31:00+05:30"})
    )
    info = provider.current()
    assert info.token == "filetok" and info.source == "file"
    client = SimpleNamespace(tokens=[], set_access_token=lambda t: client.tokens.append(t))
    shared = SharedKiteClient("key", provider, client=client)
    shared.refresh()
    shared.refresh()
    assert client.tokens == ["filetok"]
    path.write_text("{broken")
    assert AccessTokenProvider(token_file=str(path)).current().source == "none"
    with pytest.raises(ValueError):
        SharedKiteClient("", provider, client=client)


class FakeEngine:
    def __init__(self):
        self.user_paused = False
        self.closed = []

    def status_text(self):
        return "engine ok"

    def strategies_text(self):
        return "strategies"

    def positions_text(self):
        return "positions"

    def pnl_text(self):
        return "pnl"

    def capital_text(self):
        return "capital"

    def close_one(self, pid, user_id=None):
        self.closed.append(pid)
        return f"Closed {pid}"


def _ops(engine):
    ops = OperationsController(
        allowlist=TelegramAllowlist(user_ids=frozenset({1}), chat_ids=frozenset({2})),
        mode=ModeController(),
    )
    ops.engine = engine
    ops.readiness_reason = lambda: "broker not reconciled"
    return ops


def test_telegram_controls_drive_engine_without_opening_live_gates():
    engine = FakeEngine()
    ops = _ops(engine)
    assert "engine ok" in ops.handle("/status", user_id=1, chat_id=2)
    ops.handle("/pause", user_id=1, chat_id=2)
    assert engine.user_paused and ops.paused
    reply = ops.handle("/resume", user_id=1, chat_id=2, broker_ready=False, risk_clear=False)
    assert not engine.user_paused and ops.paused and "broker not reconciled" in reply
    ops.startup_recovery_complete = True
    reply = ops.handle("/resume", user_id=1, chat_id=2, broker_ready=True, risk_clear=True)
    assert not ops.paused and "Live readiness gates passed" in reply
    assert ops.handle("/strategies", user_id=1, chat_id=2) == "strategies"
    assert ops.handle("/pnl", user_id=1, chat_id=2) == "pnl"
    assert "Usage" in ops.handle("/close abc", user_id=1, chat_id=2)
    assert ops.handle("/close abc confirm", user_id=1, chat_id=2) == "Closed abc"
    with pytest.raises(PermissionError):
        ops.handle("/close abc confirm", user_id=9, chat_id=2)


def test_live_still_needs_request_and_separate_confirmation():
    ops = _ops(FakeEngine())
    ops.startup_recovery_complete = True
    ops.handle("/resume", user_id=1, chat_id=2, broker_ready=True, risk_clear=True)
    ops.handle("/live", user_id=1, chat_id=2)
    assert ops.mode.mode.value == "paper"
    with pytest.raises(RuntimeError):
        ops.handle("/confirm_live", user_id=1, chat_id=2, broker_ready=False, risk_clear=True)
    ops.handle("/confirm_live", user_id=1, chat_id=2, broker_ready=True, risk_clear=True)
    assert ops.mode.mode.value == "live"
    ops.handle("/paper", user_id=1, chat_id=2)
    assert ops.mode.mode.value == "paper" and ops.paused


def test_help_lists_every_registered_command_and_start_aliases_help():
    from sensex_engine.telegram_control import COMMAND_HELP, help_text
    from sensex_engine.telegram_transport import COMMANDS

    ops = _ops(FakeEngine())
    text = ops.handle("/help", user_id=1, chat_id=2)
    assert ops.handle("/start", user_id=1, chat_id=2) == text == help_text()
    for name, menu, _ in COMMAND_HELP:
        assert f"/{name} - " in text
        assert name in COMMANDS and 0 < len(menu) <= 256 and name.islower()
    assert "start" in COMMANDS


def test_alert_gate_mutes_alternating_reasons_of_one_outage():
    from sensex_engine.app import AlertGate

    now = [0.0]
    gate = AlertGate(repeat_after_seconds=6 * 3600, clock=lambda: now[0])
    assert gate.should_send(False, "not authenticated")
    # The live failure mode: two reasons alternating every ~30 s for hours.
    for t in range(30, 3 * 3600, 30):
        now[0] = t
        reason = "data is stale" if t % 60 else "not authenticated"
        assert not gate.should_send(False, reason)
    assert gate.should_send(True, "recovered")
    assert not gate.should_send(True, "recovered")
    now[0] += 10
    assert gate.should_send(False, "data is stale")  # first problem after recovery
    now[0] += 6 * 3600 + 1
    assert gate.should_send(False, "data is stale")  # 6-hourly reminder


def test_token_is_refreshed_even_outside_market_hours(tmp_path):
    from datetime import datetime

    from engine_fakes import FakeKite

    from sensex_engine.book import EngineBook
    from sensex_engine.engine import Engine, EngineConfig, Notifier
    from sensex_engine.indicators import IST
    from sensex_engine.kite import KiteReadOnlyAdapter
    from sensex_engine.market_feed import MarketFeed

    calls = []
    saturday = datetime(2026, 11, 28, 6, 31, tzinfo=IST)
    engine = Engine(
        feed=MarketFeed(KiteReadOnlyAdapter(FakeKite())), book=EngineBook(tmp_path / "e.db"),
        notifier=Notifier(), config=EngineConfig(), clock=lambda: saturday,
        token_refresh=lambda: calls.append(1) or SimpleNamespace(token="new"),
    )
    engine.cycle()
    assert calls == [1]
