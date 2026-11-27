import asyncio
from types import SimpleNamespace

import pytest

from sensex_engine.domain import TradingMode
from sensex_engine.mode import ModeController
from sensex_engine.persistence import SqliteStore
from sensex_engine.telegram_auth import TelegramAllowlist, parse_id_allowlist
from sensex_engine.telegram_control import OperationsController
from sensex_engine.telegram_transport import (
    _sqlite_database_path,
    build_supervised_monitor_from_settings,
    dispatch_command,
    operations_from_settings,
    run_startup_recovery_from_settings,
)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, value):
        self.replies.append(value)


def _update(update_id, text, user_id=11, chat_id=22):
    message = FakeMessage(text)
    update = SimpleNamespace(
        update_id=update_id,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return update, message


def _operations(mode=TradingMode.PAPER, paused=True):
    return OperationsController(
        TelegramAllowlist(frozenset({11}), frozenset({22})),
        ModeController(mode=mode),
        paused=paused,
    )


def test_parse_telegram_allowlists_and_reject_malformed_values():
    assert parse_id_allowlist("11, 12, -22") == frozenset({11, 12, -22})
    assert parse_id_allowlist("") == frozenset()
    try:
        parse_id_allowlist("11,abc")
    except ValueError as exc:
        assert "integer IDs" in str(exc)
    else:
        raise AssertionError("malformed allowlist must be rejected")


def test_transport_audits_and_dispatches_authorized_paper_command(tmp_path):
    store = SqliteStore(tmp_path / "telegram.sqlite3")
    operations = _operations(mode=TradingMode.LIVE, paused=False)
    update, message = _update(101, "/paper")
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert operations.mode.mode is TradingMode.PAPER
    assert operations.paused
    assert "Paper mode" in message.replies[0]
    audit = store.command_audit_for("telegram:101")
    assert audit is not None and audit["outcome"] == "ACCEPTED"


def test_transport_ignores_duplicate_updates_without_reexecuting(tmp_path):
    store = SqliteStore(tmp_path / "telegram.sqlite3")
    operations = _operations()
    update, first = _update(102, "/paper")
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    operations.paused = False
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert operations.paused is False
    assert first.replies[-1] == "Duplicate command ignored."


def test_transport_denies_non_allowlisted_user_and_records_audit(tmp_path):
    store = SqliteStore(tmp_path / "telegram.sqlite3")
    operations = _operations(paused=False)
    update, message = _update(103, "/pause", user_id=999)
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert operations.paused is False
    assert "not authorized" in message.replies[0]
    audit = store.command_audit_for("telegram:103")
    assert audit is not None and audit["outcome"] == "DENIED"


def test_resume_requires_explicit_readiness_provider(tmp_path):
    store = SqliteStore(tmp_path / "telegram.sqlite3")
    operations = _operations()
    update, message = _update(104, "/resume")
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert operations.paused
    assert "startup recovery" in message.replies[0]

    operations.startup_recovery_complete = True
    update, message = _update(105, "/resume")
    asyncio.run(
        dispatch_command(
            update, None, operations=operations, store=store,
            readiness_provider=lambda: (True, True),
        )
    )
    assert not operations.paused
    assert "resumed" in message.replies[0]


def test_transport_fails_closed_when_audit_storage_is_unavailable():
    class BrokenStore:
        def record_command(self, **kwargs):
            raise OSError("database unavailable")

    operations = _operations(paused=False)
    update, message = _update(106, "/paper")
    asyncio.run(
        dispatch_command(update, None, operations=operations, store=BrokenStore())
    )
    assert operations.paused is False
    assert message.replies[0].startswith("Command rejected: audit storage")


def test_operations_factory_uses_configured_telegram_allowlists():
    settings = SimpleNamespace(
        telegram_allowed_user_ids="11,12",
        telegram_allowed_chat_ids="-22",
    )
    operations = operations_from_settings(settings)
    assert operations.allowlist.allows(11, -22)
    assert not operations.allowlist.allows(13, -22)



def test_telegram_entrypoint_accepts_only_sqlite_database_urls():
    assert _sqlite_database_path("sqlite:///./portfolio.db") == "./portfolio.db"
    assert _sqlite_database_path("sqlite:////var/lib/portfolio.db") == "/var/lib/portfolio.db"
    try:
        _sqlite_database_path("sqlite:///:memory:")
    except ValueError as exc:
        assert "In-memory SQLite is unsupported" in str(exc)
    else:
        raise AssertionError("ephemeral audit storage must be rejected")
    try:
        _sqlite_database_path("postgresql://localhost/portfolio")
    except ValueError as exc:
        assert "SQLite DATABASE_URL" in str(exc)
    else:
        raise AssertionError("unsupported database URL must be rejected")


class StartupBroker:
    def __init__(self, authenticated=True):
        self.authenticated = authenticated
        self.trade_history_calls = []

    def is_authenticated(self):
        return self.authenticated

    def instruments(self):
        return [SimpleNamespace(
            instrument_token=1001,
            tradingsymbol="SENSEX_TEST_CE",
            lot_size=10,
        )]

    def positions(self):
        return {"net": []}

    def orders(self):
        return []

    def order_trades(self, broker_order_id):
        self.trade_history_calls.append(broker_order_id)
        return []


def test_runtime_startup_runs_read_only_recovery_before_marking_gate_complete(tmp_path):
    store = SqliteStore(tmp_path / "startup-runtime.sqlite3")
    operations = _operations()
    broker = StartupBroker()
    settings = SimpleNamespace(
        kite_api_key="api-key",
        kite_access_token="access-token",
    )

    result = run_startup_recovery_from_settings(
        settings,
        store=store,
        operations=operations,
        broker_factory=lambda api_key, access_token: broker,
    )

    assert result is not None and result.recovery_complete
    assert operations.startup_recovery_complete
    assert broker.trade_history_calls == []


def test_runtime_startup_without_kite_credentials_keeps_gate_closed(tmp_path):
    store = SqliteStore(tmp_path / "startup-no-creds.sqlite3")
    operations = _operations()
    settings = SimpleNamespace(kite_api_key="", kite_access_token="")

    result = run_startup_recovery_from_settings(
        settings,
        store=store,
        operations=operations,
        broker_factory=lambda *_: pytest.fail("broker must not be constructed"),
    )

    assert result is None
    assert not operations.startup_recovery_complete


def test_runtime_startup_reconciliation_failure_keeps_gate_closed(tmp_path):
    store = SqliteStore(tmp_path / "startup-fail.sqlite3")
    operations = _operations()
    broker = StartupBroker(authenticated=False)
    settings = SimpleNamespace(
        kite_api_key="api-key",
        kite_access_token="access-token",
    )

    result = run_startup_recovery_from_settings(
        settings,
        store=store,
        operations=operations,
        broker_factory=lambda *_: broker,
    )

    assert result is not None and not result.recovery_complete
    assert not operations.startup_recovery_complete



def test_supervised_monitor_forces_paper_and_pauses_on_broker_mismatch(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "supervised-monitor.sqlite3")
        operations = _operations(mode=TradingMode.LIVE, paused=False)
        broker = StartupBroker(authenticated=False)
        settings = SimpleNamespace(
            kite_api_key="api-key",
            kite_access_token="access-token",
            kite_monitor_poll_interval_seconds=10,
            kite_monitor_max_stale_seconds=30,
            kite_monitor_retry_max_seconds=5,
        )
        alerts = []
        monitor = build_supervised_monitor_from_settings(
            settings,
            store=store,
            operations=operations,
            alert_sender=lambda healthy, reason: alerts.append((healthy, reason)),
            broker_factory=lambda *_: broker,
        )
        assert monitor is not None
        await monitor.start()
        for _ in range(100):
            if alerts:
                break
            await asyncio.sleep(0.01)
        assert alerts and alerts[0][0] is False
        assert operations.mode.mode is TradingMode.PAPER
        assert operations.paused
        assert not operations.startup_recovery_complete
        await monitor.stop()

    asyncio.run(scenario())


def test_supervised_monitor_is_not_created_without_broker_credentials(tmp_path):
    store = SqliteStore(tmp_path / "supervised-no-creds.sqlite3")
    operations = _operations()
    monitor = build_supervised_monitor_from_settings(
        SimpleNamespace(kite_api_key="", kite_access_token=""),
        store=store,
        operations=operations,
        broker_factory=lambda *_: pytest.fail("broker must not be constructed"),
    )
    assert monitor is None



def test_emergency_command_is_allowlisted_audited_and_forces_paper(tmp_path):
    store = SqliteStore(tmp_path / "telegram-emergency.sqlite3")
    operations = _operations(mode=TradingMode.LIVE, paused=False)
    calls = []

    class Emergency:
        def execute(self):
            calls.append("executed")
            operations.paused = True
            operations.mode.force_paper()
            return SimpleNamespace(completed=True, to_text=lambda: "Emergency close complete")

    operations.emergency_close_all = Emergency()
    update, message = _update(301, "/emergency")
    asyncio.run(dispatch_command(
        update, None, operations=operations, store=store,
        readiness_provider=lambda: (_ for _ in ()).throw(
            AssertionError("Emergency command must not wait on entry readiness")
        ),
    ))
    assert calls == ["executed"]
    assert operations.paused
    assert operations.mode.mode is TradingMode.PAPER
    assert message.replies[0] == "Emergency close complete"
    assert store.command_audit_for("telegram:301")["outcome"] == "ACCEPTED"


def test_emergency_command_without_kite_service_still_halts_and_audits(tmp_path):
    store = SqliteStore(tmp_path / "telegram-emergency-unavailable.sqlite3")
    operations = _operations(mode=TradingMode.LIVE, paused=False)
    update, message = _update(302, "/closeall")
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert operations.paused
    assert operations.mode.mode is TradingMode.PAPER
    assert "no broker order was sent" in message.replies[0]
    assert store.command_audit_for("telegram:302")["outcome"] == "BLOCKED"


def test_emergency_command_rejects_unauthorized_user_before_execution(tmp_path):
    store = SqliteStore(tmp_path / "telegram-emergency-denied.sqlite3")
    operations = _operations(mode=TradingMode.LIVE, paused=False)
    calls = []

    class Emergency:
        def execute(self):
            calls.append("executed")
            return SimpleNamespace(completed=True, to_text=lambda: "closed")

    operations.emergency_close_all = Emergency()
    update, message = _update(303, "/emergency", user_id=999)
    asyncio.run(dispatch_command(update, None, operations=operations, store=store))
    assert calls == []
    assert "not authorized" in message.replies[0]
    assert store.command_audit_for("telegram:303")["outcome"] == "DENIED"



def test_live_resume_and_confirmation_are_blocked_during_emergency():
    operations = _operations()
    operations.startup_recovery_complete = True
    operations.emergency_active = True
    for command in ("/live", "/confirm_live", "/resume"):
        with pytest.raises(RuntimeError, match="Emergency close-all is in progress"):
            operations.handle(command, user_id=11, chat_id=22, broker_ready=True, risk_clear=True)
    assert operations.paused
