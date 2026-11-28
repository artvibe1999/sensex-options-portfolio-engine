"""Regression tests for the second code review (2026-11-28, 10 findings)."""

import asyncio
import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from engine_fakes import NOW, SPOT, FakeKite

from sensex_engine import strategies as S
from sensex_engine.arbitration import arbitrate
from sensex_engine.book import BookLeg, BookPosition, EngineBook
from sensex_engine.engine import Engine, EngineConfig, LiveEntryRejected, Notifier
from sensex_engine.kite import KiteReadOnlyAdapter
from sensex_engine.market_data import Quote
from sensex_engine.market_feed import MarketFeed
from sensex_engine.mode import ModeController
from sensex_engine.telegram_auth import TelegramAllowlist
from sensex_engine.telegram_control import OperationsController


def _engine(tmp_path, fk=None, **cfg):
    fk = fk or FakeKite(funds=50000)
    alerts = []
    engine = Engine(
        feed=MarketFeed(KiteReadOnlyAdapter(fk)),
        book=EngineBook(tmp_path / "e.db"),
        notifier=Notifier(alerts.append),
        config=EngineConfig(**cfg),
        clock=lambda: fk.now,
    )
    engine.feed.refresh_contracts(NOW)
    return engine, fk, alerts


def _atm_call(engine):
    from engine_fakes import NEAR

    return next(
        c
        for c in engine.feed.contracts
        if c.expiry == NEAR and c.right.value == "CE" and int(c.strike) == int(SPOT)
    )


def _leg_in_position(engine, pid="p1", mode="paper", entry=200.0):
    c = _atm_call(engine)
    return BookPosition(
        id=pid,
        mode=mode,
        strategy_id=S.LEG_IN_CALL,
        status="OPEN",
        opened_at=NOW.isoformat(),
        session_day=NOW.date().isoformat(),
        lots=1,
        lot_size=20,
        entry_net=Decimal(str(entry * 20)),
        max_loss=Decimal(str(entry * 20)),
        max_profit=None,
        entry_costs=Decimal("25"),
        legs=[
            BookLeg(
                c.instrument_token,
                c.tradingsymbol,
                c.right.value,
                float(c.strike),
                c.expiry.isoformat(),
                "BUY",
                20,
                entry,
                0.1,
            )
        ],
        exit_plan={
            "kind": "leg_in",
            "stop_loss_pct": 0.25,
            "hedge_trigger": 0.40,
            "lock_target_width": None,
            "ema_exit": False,
            "alignment": "bullish",
            "profit_target_pct": 0.0,
            "profit_basis": "lock_width",
        },
        meta={"stage": "single", "managed_risk": "1000"},
    )


def test_empty_bid_or_stale_quote_is_unpriced_not_zero(tmp_path):
    engine, fk, _ = _engine(tmp_path)
    p = _leg_in_position(engine)
    token = p.legs[0].instrument_token
    snap = engine.feed.snapshot(NOW)
    q = snap.quotes[token]
    snap.quotes[token] = Quote(token, q.timestamp, q.last_price, Decimal("0"), q.ask, 0, 20, 10)
    assert engine._marks(p, snap) is None
    snap.quotes[token] = Quote(
        token, NOW - timedelta(minutes=10), q.last_price, q.bid, q.ask, 20, 20, 10
    )
    assert engine._marks(p, snap) is None


def test_traded_today_exclusion_does_not_count_as_open():
    ev = [SimpleNamespace(eligible=True, strategy_id=S.LEG_IN_PUT, priority=2, proposal=object())]
    result = arbitrate(ev, open_strategies=[], max_concurrent=2, exclude=frozenset({S.LEG_IN_CALL}))
    assert result.selected is not None  # a closed call must not block the put
    blocked = arbitrate(ev, open_strategies=[], max_concurrent=2, exclude=frozenset({S.LEG_IN_PUT}))
    assert blocked.selected is None and "already traded today" in blocked.considered[0]


def test_telegram_handlers_run_off_the_event_loop():
    from sensex_engine.telegram_transport import dispatch_command

    seen = {}

    class Ops(OperationsController):
        def handle(self, command, **kw):
            seen["thread"] = threading.current_thread()
            return "ok"

    ops = Ops(
        allowlist=TelegramAllowlist(user_ids=frozenset({1}), chat_ids=frozenset({2})),
        mode=ModeController(),
    )

    class Store:
        def record_command(self, **kw):
            return True

        def update_command_outcome(self, **kw):
            return True

    async def reply(text):
        seen["reply"] = text

    update = SimpleNamespace(
        effective_message=SimpleNamespace(text="/status", reply_text=reply),
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        update_id=1,
    )

    async def main():
        seen["loop_thread"] = threading.current_thread()
        await dispatch_command(update, None, operations=ops, store=Store())

    asyncio.run(main())
    assert seen["reply"] == "ok" and seen["thread"] is not seen["loop_thread"]


def test_emergency_runs_under_engine_lock_and_syncs_book():
    from sensex_engine.telegram_transport import _run_emergency

    calls = []

    class FakeEngine:
        _lock = threading.RLock()

        def sync_live_book(self, reason):
            calls.append(("sync", self._lock._is_owned()))

    class Emergency:
        def execute(self):
            calls.append(("execute", FakeEngine._lock._is_owned()))
            return "report"

    assert _run_emergency(Emergency(), FakeEngine()) == "report"
    assert calls == [("execute", True), ("sync", True)]


def test_live_hedge_is_attempted_once(tmp_path):
    engine, fk, alerts = _engine(tmp_path)
    p = _leg_in_position(engine, mode="live", entry=50.0)
    p.broker_position_id = "x"
    engine.book.open_position(p)
    attempts = []

    class Bridge:
        def hedge(self, *a, **k):
            attempts.append(1)
            raise RuntimeError("limit rejected")

    engine.live = Bridge()
    snap = engine.feed.snapshot(NOW)
    engine._lock_hedge(engine.book.get("p1"), snap, NOW, 50.0)
    engine._lock_hedge(engine.book.get("p1"), snap, NOW, 50.0)
    assert attempts == [1]
    assert engine.book.get("p1").meta.get("hedge_attempted")


def test_rejected_live_entry_does_not_halt_trading(tmp_path):
    engine, fk, alerts = _engine(tmp_path)
    ops = OperationsController(
        allowlist=TelegramAllowlist(user_ids=frozenset({1}), chat_ids=frozenset({2})),
        mode=ModeController(),
    )
    ops.paused = False
    engine.operations = ops

    class Bridge:
        def open(self, *a, **k):
            raise LiveEntryRejected("Strategy signal is stale or future-dated")

    engine.live = Bridge()
    from engine_fakes import FAR, NEAR, bullish_state, chain

    ctx = S.MarketContext(
        state=bullish_state(),
        near=chain(NEAR),
        far=chain(FAR),
        oi={},
        now=NOW,
        equity=Decimal("500000"),
    )
    proposal, _, _ = S.build_leg_in_call(ctx, S.StrategyConfig(), Decimal("7500"))
    from sensex_engine.capital_policy import risk_policy

    policy = risk_policy(equity=Decimal("500000"), peak_equity=Decimal("500000"))
    engine._open(proposal, 1, Decimal("5000"), "live", policy, bullish_state(), NOW)
    assert not engine.user_paused and not ops.paused
    assert "live entry skipped" in engine.last_arbitration


def test_setcapital_with_realised_pnl_today_reads_exactly_the_amount(tmp_path):
    engine, fk, _ = _engine(tmp_path)
    engine.book.set_state("paper_base_capital", "50000")
    engine.start_of_day_equity("paper", NOW.date())
    p = _leg_in_position(engine)
    engine.book.open_position(p)
    engine.book.close_position(
        "p1",
        exit_prices={p.legs[0].instrument_token: 0.05},
        exit_costs=Decimal("10"),
        reason="x",
        closed_at=NOW,
    )
    assert engine.book.realized_total("paper", day=NOW.date().isoformat()) < -3000
    engine.set_paper_capital(Decimal("100000"))
    snap, _ = engine.portfolio("paper", NOW)
    assert snap.net_liquidation_equity == Decimal("100000")


def test_daily_stop_alerts_once(tmp_path):
    engine, fk, alerts = _engine(tmp_path)
    engine.book.set_state("paper_base_capital", "50000")
    engine.start_of_day_equity("paper", NOW.date())
    for pid in ("a", "b"):
        engine.book.open_position(_leg_in_position(engine, pid=pid, entry=600.0))
    snap = engine.feed.snapshot(NOW)
    engine.last_snapshot = snap
    engine._manage_positions(NOW, snap, None, False, {})
    engine._manage_positions(NOW, snap, None, False, {})
    assert sum("Daily loss stop hit" in a for a in alerts) == 1


def test_paper_reply_matches_behaviour():
    ops = OperationsController(
        allowlist=TelegramAllowlist(user_ids=frozenset({1}), chat_ids=frozenset({2})),
        mode=ModeController(),
    )
    assert "Paper entries continue" in ops.handle("/paper", user_id=1, chat_id=2)


def test_orphan_live_leg_in_is_adopted(tmp_path):
    engine, fk, alerts = _engine(tmp_path)
    c = _atm_call(engine)

    class Positions:
        def active_snapshots(self):
            return [
                {
                    "position_id": "orphan-1",
                    "mode": "live",
                    "strategy_id": S.LEG_IN_CALL,
                    "status": "OPEN",
                    "legs": [
                        {
                            "instrument_token": c.instrument_token,
                            "side": "BUY",
                            "remaining_quantity": 20,
                            "open_average_price": "150",
                        }
                    ],
                },
                {
                    "position_id": "odd",
                    "mode": "live",
                    "strategy_id": "OTHER",
                    "status": "OPEN",
                    "legs": [],
                },
            ]

    engine.live = SimpleNamespace(positions=Positions())
    reports = engine.adopt_orphan_live_positions()
    assert reports[0].startswith("adopted orphan-1") and "UNMANAGED" in reports[1]
    adopted = engine.book.open_positions("live")[0]
    assert adopted.broker_position_id == "orphan-1" and adopted.exit_plan["kind"] == "leg_in"
    assert engine.adopt_orphan_live_positions() == ["UNMANAGED durable live position odd (OTHER)"]
    assert SPOT
