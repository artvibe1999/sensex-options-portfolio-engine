"""Regression tests for the code-review findings on the strategy engine."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from engine_fakes import NOW, SPOT, FakeKite, bullish_state, make_state

from sensex_engine import reconciliation
from sensex_engine import strategies as S
from sensex_engine.book import BookLeg, BookPosition, EngineBook
from sensex_engine.capital_policy import risk_policy
from sensex_engine.domain import Regime
from sensex_engine.engine import Engine, EngineConfig, Notifier
from sensex_engine.kite import KiteReadOnlyAdapter
from sensex_engine.kite_execution import KiteOrderExecutionAdapter
from sensex_engine.market_feed import MarketFeed
from sensex_engine.mode import ModeController
from sensex_engine.telegram_auth import TelegramAllowlist
from sensex_engine.telegram_control import OperationsController
from sensex_engine.telegram_transport import dispatch_command


class Store:
    def __init__(self):
        self.rows = {}

    def record_command(self, *, command_id, **kw):
        if command_id in self.rows:
            return False
        self.rows[command_id] = kw
        return True

    def update_command_outcome(self, *, command_id, outcome, detail):
        self.rows[command_id]["outcome"] = outcome
        return True


def _update(text, update_id=1):
    replies = []

    async def reply_text(value):
        replies.append(value)

    message = SimpleNamespace(text=text, reply_text=reply_text)
    return (
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=2),
            update_id=update_id,
        ),
        replies,
    )


class Eng:
    def __init__(self):
        self.user_paused = False
        self.closed = []

    def close_one(self, pid, user_id=None):
        self.closed.append(pid)
        return f"Closed {pid}"

    def close_all(self, mode, reason):
        return 0


def _ops(engine):
    ops = OperationsController(
        allowlist=TelegramAllowlist(user_ids=frozenset({1}), chat_ids=frozenset({2})),
        mode=ModeController(),
    )
    ops.engine = engine
    return ops


def test_close_arguments_reach_the_controller_through_dispatch():
    engine = Eng()
    update, replies = _update("/close@SensexHedgeBot abc123 confirm")
    asyncio.run(dispatch_command(update, None, operations=_ops(engine), store=Store()))
    assert engine.closed == ["abc123"] and replies == ["Closed abc123"]


def test_emergency_pauses_engine_even_when_broker_close_raises():
    engine = Eng()
    ops = _ops(engine)

    class Boom:
        def execute(self):
            raise RuntimeError("kite down")

    ops.emergency_close_all = Boom()
    update, replies = _update("/emergency")
    asyncio.run(dispatch_command(update, None, operations=ops, store=Store()))
    assert engine.user_paused and ops.paused and ops.mode.mode.value == "paper"


@pytest.mark.parametrize(
    "regime,bias,sid,ok",
    [
        (Regime.TRANSITION, "neutral", S.BULL_CALL, False),
        (Regime.UNKNOWN, "neutral", S.LONG_IRON_BUTTERFLY, False),
        (Regime.TRANSITION, "neutral", S.LONG_IRON_BUTTERFLY, True),
        (Regime.BULL_TREND, "bullish", S.IRON_CONDOR, False),
        (Regime.RANGE, "neutral", S.IRON_CONDOR, True),
        (Regime.RANGE, "neutral", S.BULL_PUT, True),
        (Regime.RANGE, "neutral", S.BULL_CALL, False),
        (Regime.VOLATILITY_EXPANSION, "bearish", S.BEAR_PUT, True),
        (Regime.VOLATILITY_EXPANSION, "bearish", S.BULL_CALL, False),
    ],
)
def test_live_regime_gates_every_family(regime, bias, sid, ok):
    assert S.regime_check(sid, make_state(regime=regime, bias=bias)).passed is ok


def test_bull_call_blocked_when_classifier_says_transition():
    from engine_fakes import FAR, NEAR, chain

    ctx = S.MarketContext(
        state=bullish_state(regime=Regime.TRANSITION),
        near=chain(NEAR),
        far=chain(FAR),
        oi={},
        now=NOW,
    )
    first = S.evaluate_all(ctx, risk_budget=Decimal("9000"))[0]
    assert not first.eligible and first.checks[0].name == "regime supports thesis"


def test_managed_scope_still_sees_engine_orphan_fills():
    reconciliation.configure_scope(managed=True)
    try:
        contracts = [SimpleNamespace(instrument_token=7, tradingsymbol="SENSEXX", lot_size=20)]
        report = reconciliation.reconcile_snapshots(
            contracts=contracts,
            internal_positions=[],
            broker_positions={"net": [{"exchange": "BFO", "instrument_token": 7, "quantity": -20}]},
            broker_orders=[{"status": "COMPLETE", "tag": "PF0123", "tradingsymbol": "SENSEXX"}],
        )
        assert not report.reconciled
    finally:
        reconciliation.configure_scope(managed=False)


def test_legacy_sx_tag_is_recognised_for_recovery():
    tags = KiteOrderExecutionAdapter.known_tags_for_client_key("key-1")
    assert tags[0].startswith("PF") and tags[1] == "SX" + tags[0][2:]


def _position(pid, opened_day, **kw):
    base = dict(
        id=pid,
        mode="paper",
        strategy_id=S.BULL_PUT,
        status="OPEN",
        opened_at=f"{opened_day}T10:00:00+05:30",
        session_day=opened_day,
        lots=1,
        lot_size=20,
        entry_net=Decimal("-1000"),
        max_loss=Decimal("3000"),
        max_profit=Decimal("1000"),
        entry_costs=Decimal("10"),
        legs=[
            BookLeg(1, "A", "PE", 1.0, "x", "BUY", 20, 10.0, 0.1),
            BookLeg(2, "B", "PE", 2.0, "x", "SELL", 20, 60.0, 0.1),
        ],
        exit_plan={"profit_target_pct": 0.5, "profit_basis": "max_profit", "stop_loss_pct": 1.0},
    )
    base.update(kw)
    return BookPosition(**base)


def test_realised_pnl_is_counted_on_the_close_date(tmp_path):
    book = EngineBook(tmp_path / "b.db")
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    book.open_position(_position("carry", yesterday))
    book.close_position(
        "carry", exit_prices={1: 0.0, 2: 250.0}, exit_costs=Decimal("10"), reason="x", closed_at=NOW
    )
    assert book.realized_total("paper", day=NOW.date().isoformat()) < 0
    assert book.realized_total("paper", day=yesterday) == 0
    assert book.closed_positions(mode="paper", since_day=NOW.date().isoformat())


def test_operator_stops_can_only_tighten_tier_stops():
    tier = risk_policy(equity=Decimal("80000"), peak_equity=Decimal("80000"))
    tight = risk_policy(
        equity=Decimal("80000"), peak_equity=Decimal("80000"), daily_stop_override=Decimal("0.02")
    )
    loose = risk_policy(
        equity=Decimal("80000"), peak_equity=Decimal("80000"), daily_stop_override=Decimal("0.50")
    )
    assert tight.limits.daily_loss_stop_pct == Decimal("0.02")
    assert loose.limits.daily_loss_stop_pct == tier.limits.daily_loss_stop_pct


def test_explicit_settings_feed_overrides_but_defaults_do_not(tmp_path):
    from sensex_engine import app
    from sensex_engine.config import Settings

    default = app._engine_config(Settings(_env_file=None))
    explicit = app._engine_config(Settings(_env_file=None, daily_loss_stop_pct=0.02))
    assert default.daily_stop_override is None
    assert explicit.daily_stop_override == Decimal("0.02")


def _engine(tmp_path, fk):
    feed = MarketFeed(KiteReadOnlyAdapter(fk))
    alerts = []
    engine = Engine(
        feed=feed,
        book=EngineBook(tmp_path / "e.db"),
        notifier=Notifier(alerts.append),
        config=EngineConfig(enabled_strategies=frozenset({S.BULL_CALL})),
        clock=lambda: fk.now,
    )
    return engine, alerts


def test_missing_quote_defers_daily_stop_instead_of_liquidating(tmp_path):
    fk = FakeKite(funds=100000)
    engine, alerts = _engine(tmp_path, fk)
    engine.feed.refresh_contracts(NOW)
    engine.book.start_day("paper", NOW.date(), Decimal("100000"))
    engine.book.set_state("paper_base_capital", "100000")
    token = engine.feed.contracts[0].instrument_token
    position = _position(
        "p",
        NOW.date().isoformat(),
        max_loss=Decimal("9000"),
        legs=[
            BookLeg(
                token, engine.feed.contracts[0].tradingsymbol, "CE", 1.0, "x", "BUY", 20, 10.0, 0.1
            ),
            BookLeg(999999, "MISSING", "CE", 2.0, "x", "SELL", 20, 5.0, 0.1),
        ],
    )
    engine.book.open_position(position)
    snap = engine.feed.snapshot(NOW)
    engine.last_snapshot = snap
    engine._manage_positions(NOW, snap, None, False, {})
    assert engine.book.open_positions("paper")
    assert not engine.book.equity_day("paper", NOW.date())["halted"]
    assert "deferred" in engine.last_error


def test_incomplete_live_entry_pauses_and_forces_paper(tmp_path, monkeypatch):
    fk = FakeKite(funds=500000)
    engine, alerts = _engine(tmp_path, fk)
    ops = _ops(engine)
    ops.mode.mode = ops.mode.mode.__class__("live")
    ops.paused = False
    engine.operations = ops

    class Bridge:
        def open(self, *a, **k):
            raise RuntimeError("Live entry incomplete: residual=((1, -20),)")

    engine.live = Bridge()
    from engine_fakes import FAR, NEAR, chain

    ctx = S.MarketContext(state=bullish_state(), near=chain(NEAR), far=chain(FAR), oi={}, now=NOW)
    proposal, _, _ = S.build_bull_call(ctx, S.StrategyConfig(), Decimal("9000"))
    policy = risk_policy(equity=Decimal("500000"), peak_equity=Decimal("500000"))
    engine._open(proposal, 1, Decimal("5000"), "live", policy, bullish_state(), NOW)
    assert engine.user_paused and ops.paused and ops.mode.mode.value == "paper"
    assert not engine.book.open_positions() and "PAUSED" in alerts[-1]


def test_operator_close_refuses_stale_snapshot(tmp_path):
    fk = FakeKite()
    engine, _ = _engine(tmp_path, fk)
    engine.feed.refresh_contracts(NOW)
    engine.last_snapshot = engine.feed.snapshot(NOW)
    fk.now = NOW + timedelta(hours=5)
    engine.book.open_position(_position("q", NOW.date().isoformat()))
    assert "stale" in engine.close_one("q")
    with pytest.raises(RuntimeError, match="stale"):
        engine.close_all("paper", "operator")
    assert SPOT > 0


@pytest.mark.parametrize("text,expected", [
    ("200000", "200000"), ("2,00,000", "200000"), ("Rs 50000", "50000"), ("50k", "50000"),
    ("2.5L", "250000"), ("2.5 lakh", "250000"), ("1cr", "10000000"), ("₹75000", "75000"),
    ("abc", None), ("-5", None), ("0", None), ("1e400", None),
])
def test_parse_amount(text, expected):
    from sensex_engine.telegram_control import parse_amount

    result = parse_amount(text)
    assert (None if result is None else str(int(result))) == expected


def test_setcapital_resets_paper_equity_and_policy(tmp_path):
    fk = FakeKite(funds=50000)
    engine, alerts = _engine(tmp_path, fk)
    yesterday = (NOW - timedelta(days=1)).date()
    engine.book.set_state("paper_base_capital", "50000")
    engine.book.start_day("paper", yesterday, Decimal("60000"))  # old peak
    engine.book.open_position(_position("old", yesterday.isoformat()))
    engine.book.close_position("old", exit_prices={1: 0.0, 2: 70.0}, exit_costs=Decimal("10"),
                               reason="x", closed_at=NOW - timedelta(days=1))
    realised = engine.book.realized_total("paper")
    ops = _ops(engine)
    reply = ops.handle("/setcapital 2.5L", user_id=1, chat_id=2)
    assert "Paper capital set to Rs 250,000.00" in reply and "Capital tier: small" in reply
    assert Decimal(engine.book.get_state("paper_base_capital")) == Decimal("250000") - realised
    engine.feed.refresh_contracts(NOW)
    snap, policy = engine.portfolio("paper", NOW)
    assert snap.net_liquidation_equity == Decimal("250000")
    assert snap.peak_equity == Decimal("250000") and policy.drawdown_pct == 0
    assert engine.book.closed_positions(mode="paper")  # history kept
    assert any("Capital set" in a for a in alerts)
    assert "Usage" in ops.handle("/setcapital", user_id=1, chat_id=2)
    assert "Could not read" in ops.handle("/setcapital lots", user_id=1, chat_id=2)
    assert "between" in ops.handle("/setcapital 500", user_id=1, chat_id=2)
    engine.book.open_position(_position("open", NOW.date().isoformat()))
    assert "Not changed" in ops.handle("/setcapital 100000", user_id=1, chat_id=2)
    with pytest.raises(PermissionError):
        ops.handle("/setcapital 100000", user_id=9, chat_id=2)
