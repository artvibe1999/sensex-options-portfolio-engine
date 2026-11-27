import asyncio
from types import SimpleNamespace

from sensex_engine.domain import TradingMode
from sensex_engine.mode import ModeController
from sensex_engine.persistence import SqliteStore
from sensex_engine.telegram_auth import TelegramAllowlist, parse_id_allowlist
from sensex_engine.telegram_control import OperationsController
from sensex_engine.telegram_transport import (
    _sqlite_database_path,
    dispatch_command,
    operations_from_settings,
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
    assert "readiness" in message.replies[0]

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
    assert _sqlite_database_path("sqlite:///:memory:") == ":memory:"
    try:
        _sqlite_database_path("postgresql://localhost/portfolio")
    except ValueError as exc:
        assert "SQLite DATABASE_URL" in str(exc)
    else:
        raise AssertionError("unsupported database URL must be rejected")
