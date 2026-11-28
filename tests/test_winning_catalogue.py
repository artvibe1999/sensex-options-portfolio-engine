"""Winning-only catalogue: leg-in lock plan, credit-spread capital gate, live hedge guard."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from engine_fakes import FAR, NEAR, NOW, SPOT, FakeKite, bullish_state, chain, make_state

from sensex_engine import strategies as S
from sensex_engine.book import EngineBook
from sensex_engine.capital_policy import risk_policy
from sensex_engine.domain import Regime
from sensex_engine.engine import Engine, EngineConfig, LiveBridge, LiveEntryError, Notifier
from sensex_engine.indicators import StructureState
from sensex_engine.kite import KiteReadOnlyAdapter
from sensex_engine.market_feed import MarketFeed


def ctx(state, equity):
    return S.MarketContext(
        state=state, near=chain(NEAR), far=chain(FAR), oi={}, now=NOW, equity=Decimal(equity)
    )


def test_catalogue_contains_only_the_winning_families():
    assert S.PRIORITY == (S.LEG_IN_CALL, S.LEG_IN_PUT, S.BULL_PUT, S.BEAR_CALL)
    assert set(S.SETUPS) == set(S.BUILDERS) == set(S.PRIORITY)
    for removed in (
        "BULL_CALL",
        "BEAR_PUT",
        "IRON_CONDOR",
        "IRON_BUTTERFLY",
        "CALENDAR",
        "LONG_IRON_BUTTERFLY",
    ):
        assert not hasattr(S, removed)


@pytest.mark.parametrize(
    "equity,budget,floor", [(30000, "1200", 0.20), (50000, "2000", 0.20), (400000, "7500", 0.30)]
)
def test_leg_in_picks_highest_delta_strike_that_fits_the_capital(equity, budget, floor):
    proposal, why, _ = S.build_leg_in_call(
        ctx(bullish_state(), equity), S.StrategyConfig(), Decimal(budget)
    )
    assert proposal is not None, why
    ((a, side),) = proposal.legs
    assert side.value == "BUY" and a.contract.right.value == "CE"
    assert floor <= float(a.delta) <= 0.60
    assert proposal.sizing_risk_per_lot <= Decimal(budget)
    assert proposal.net_debit_per_lot <= Decimal(equity)
    # Stop-managed: risk = 20% of premium + charges, far below the premium itself.
    assert proposal.sizing_risk_per_lot < proposal.max_loss_per_lot
    assert proposal.exit_plan.kind == "leg_in" and proposal.exit_plan.hedge_trigger == 0.60
    smaller, _, _ = S.build_leg_in_call(
        ctx(bullish_state(), equity), S.StrategyConfig(), Decimal(budget) / 2
    )
    if smaller is not None:
        assert abs(float(smaller.legs[0][0].delta)) <= abs(float(a.delta))


def test_leg_in_reports_when_no_strike_fits():
    proposal, why, _ = S.build_leg_in_call(
        ctx(bullish_state(), 30000), S.StrategyConfig(), Decimal("100")
    )
    assert proposal is None and "no strike fits budget" in why


def test_leg_in_put_signal_and_strike():
    bear = make_state(
        ema9=SPOT + 60,
        ema21=SPOT + 150,
        ema50=SPOT + 300,
        adx=27.0,
        adx_prev=23.0,
        plus_di=12.0,
        minus_di=30.0,
        rsi=38.0,
        regime=Regime.BEAR_TREND,
        bias="bearish",
        structure=StructureState(
            (SPOT + 700, SPOT + 300),
            (SPOT + 200, SPOT + 40),
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
        ),
    )
    checks = S.setup_leg_in_put(bear, NOW)
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    proposal, why, _ = S.build_leg_in_put(ctx(bear, 50000), S.StrategyConfig(), Decimal("2000"))
    assert proposal is not None and proposal.legs[0][0].contract.right.value == "PE", why
    late = S.setup_leg_in_put(bear, NOW.replace(hour=13, minute=45))
    assert any(c.name == "session window" and not c.passed for c in late)


def test_credit_spreads_need_two_lakh_capital():
    small = S.evaluate_all(ctx(bullish_state(), 50000), risk_budget=Decimal("2000"))
    bull_put = next(e for e in small if e.strategy_id == S.BULL_PUT)
    assert not bull_put.eligible and bull_put.checks[0].name.startswith("capital >=")
    big = S.evaluate_all(ctx(bullish_state(), 500000), risk_budget=Decimal("7500"))
    assert next(e for e in big if e.strategy_id == S.BULL_PUT).checks[0].passed


@pytest.mark.parametrize(
    "regime,bias,sid,ok",
    [
        (Regime.TRANSITION, "neutral", S.LEG_IN_CALL, False),
        (Regime.BULL_TREND, "bullish", S.LEG_IN_CALL, True),
        (Regime.BEAR_TREND, "bearish", S.LEG_IN_PUT, True),
        (Regime.VOLATILITY_EXPANSION, "bullish", S.LEG_IN_CALL, True),
        (Regime.VOLATILITY_EXPANSION, "bullish", S.LEG_IN_PUT, False),
        (Regime.RANGE, "neutral", S.BULL_PUT, True),
        (Regime.UNKNOWN, "neutral", S.BEAR_CALL, False),
    ],
)
def test_regime_gate(regime, bias, sid, ok):
    assert S.regime_check(sid, make_state(regime=regime, bias=bias)).passed is ok


# ------------------------------------------------------------------ engine lifecycle
class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _enter(tmp_path, monkeypatch, funds=50000):
    monkeypatch.setitem(S.SETUPS, S.LEG_IN_CALL, lambda state, now: [])
    monkeypatch.setattr(S, "regime_check", lambda sid, state: S.Check("regime", True))
    clock = Clock(NOW)
    fk = FakeKite(funds=funds)
    alerts = []
    engine = Engine(
        feed=MarketFeed(KiteReadOnlyAdapter(fk)),
        book=EngineBook(tmp_path / "e.db"),
        notifier=Notifier(alerts.append),
        config=EngineConfig(enabled_strategies=frozenset({S.LEG_IN_CALL})),
        clock=clock,
    )
    engine.cycle()
    positions = engine.book.open_positions("paper")
    assert len(positions) == 1, engine.last_arbitration
    return engine, fk, clock, alerts, positions[0]


def _tick(engine, fk, clock, seconds, spot):
    clock.now = fk.now = clock.now + timedelta(seconds=seconds)
    fk.spot = spot
    engine.cycle()


def test_leg_in_locks_profit_then_squares_off(tmp_path, monkeypatch):
    engine, fk, clock, alerts, p = _enter(tmp_path, monkeypatch)
    assert p.strategy_id == S.LEG_IN_CALL and len(p.legs) == 1
    for step in range(1, 30):
        _tick(engine, fk, clock, 30, SPOT + 60 * step)
        p = engine.book.get(p.id)
        if p.status != "OPEN" or p.meta.get("stage") == "locked":
            break
    assert p.meta.get("stage") == "locked", (p.status, p.exit_reason)
    long_leg, short_leg = p.legs
    assert short_leg.side == "SELL" and short_leg.right == "CE"
    assert short_leg.strike > long_leg.strike and short_leg.entry_price >= long_leg.entry_price
    assert p.max_loss == 0 and p.entry_net <= 0  # cannot lose before charges
    assert any("HEDGED" in a for a in alerts)
    assert engine._open_risk(p) == 0
    # Market gives everything back: the locked spread still closes >= -charges.
    clock.now = fk.now = NOW.replace(hour=15, minute=11)
    fk.spot = SPOT - 800
    engine.cycle()
    closed = engine.book.get(p.id)
    assert closed.status == "CLOSED"
    assert closed.realized_pnl >= -(closed.entry_costs + closed.exit_costs) - Decimal("1")


def test_leg_in_premium_stop_before_hedge(tmp_path, monkeypatch):
    engine, fk, clock, alerts, p = _enter(tmp_path, monkeypatch)
    _tick(engine, fk, clock, 30, SPOT - 230)
    closed = engine.book.get(p.id)
    assert closed.status == "CLOSED" and "premium stop" in closed.exit_reason
    assert closed.realized_pnl < 0


def test_sizing_uses_full_capital_but_stop_risk_budget(tmp_path, monkeypatch):
    engine, fk, clock, alerts, p = _enter(tmp_path, monkeypatch, funds=30000)
    policy = risk_policy(equity=Decimal("30000"), peak_equity=Decimal("30000"))
    assert Decimal(p.meta["managed_risk"]) <= policy.max_trade_risk
    assert p.entry_net <= Decimal("30000")  # up to 100% of capital may be deployed
    assert engine.config.max_margin_utilization == Decimal("1.0")


# ------------------------------------------------------------------ live bridge
class FakePositions:
    def __init__(self):
        self.snaps = {}

    def snapshot(self, pid):
        return self.snaps.get(pid)


class FakeCoordinator:
    def __init__(self, positions, completed=True):
        self.positions = positions
        self.completed = completed
        self.calls = []

    def execute_sized(self, candidate, *, max_lots, allocation_pct, **kw):
        self.calls.append(("open", kw))
        pid = "11111111-1111-1111-1111-111111111111"
        leg = candidate.legs[0]
        self.positions.snaps[pid] = {
            "strategy_id": candidate.strategy_id,
            "legs": [
                {
                    "instrument_token": leg.contract.instrument_token,
                    "quantity": 20 * max_lots,
                    "open_average_price": "100",
                    "side": "BUY",
                    "tradingsymbol": "X",
                }
            ],
        }
        return SimpleNamespace(
            position_id=pid, completed=self.completed, reason="r", residual_exposure=()
        )

    def execute_protected_short(self, position, *, protecting_position_id):
        self.calls.append(("hedge", protecting_position_id))
        pid = "22222222-2222-2222-2222-222222222222"
        self.positions.snaps[pid] = {
            "strategy_id": position.strategy_id,
            "legs": [
                {
                    "instrument_token": position.legs[0].contract.instrument_token,
                    "quantity": 20,
                    "open_average_price": "101.5",
                    "side": "SELL",
                    "tradingsymbol": "Y",
                }
            ],
        }
        return SimpleNamespace(position_id=pid, completed=True, reason="", residual_exposure=())


def test_live_bridge_leg_in_passes_stop_fraction_and_guards_hedge_geometry():
    from sensex_engine.book import BookLeg, BookPosition

    proposal, _, _ = S.build_leg_in_call(
        ctx(bullish_state(), 500000), S.StrategyConfig(), Decimal("7500")
    )
    positions = FakePositions()
    coordinator = FakeCoordinator(positions)
    quotes = {a.contract.instrument_token: a.quote for a, _ in proposal.legs}
    bridge = LiveBridge(
        coordinator=coordinator,
        positions=positions,
        sizing=SimpleNamespace(risk=None),
        adapter=SimpleNamespace(quotes=lambda t: {k: quotes[k] for k in t}),
        snapshot_provider=lambda: None,
    )
    policy = risk_policy(equity=Decimal("500000"), peak_equity=Decimal("500000"))
    pid, lots, fills = bridge.open(proposal, lots=1, policy=policy, allocation_pct=Decimal("1"))
    assert coordinator.calls[0] == ("open", {"stop_fraction": Decimal("0.25")})
    long_contract = proposal.legs[0][0].contract
    book_pos = BookPosition(
        id="b",
        mode="live",
        strategy_id=S.LEG_IN_CALL,
        status="OPEN",
        opened_at="x",
        session_day="x",
        lots=1,
        lot_size=20,
        entry_net=Decimal("2000"),
        max_loss=Decimal("2000"),
        max_profit=None,
        entry_costs=Decimal("1"),
        legs=[
            BookLeg(
                long_contract.instrument_token,
                long_contract.tradingsymbol,
                "CE",
                float(long_contract.strike),
                long_contract.expiry.isoformat(),
                "BUY",
                20,
                100.0,
                0.1,
            )
        ],
        exit_plan={"kind": "leg_in"},
        broker_position_id=pid,
    )
    further = replace(
        long_contract,
        instrument_token=99,
        tradingsymbol="HIGHER",
        strike=long_contract.strike + 300,
    )
    hedge_id, price = bridge.hedge(book_pos, further, 20)
    assert coordinator.calls[-1] == ("hedge", pid) and price == 101.5
    lower = replace(further, strike=long_contract.strike - 300)
    with pytest.raises(LiveEntryError, match="protect"):
        bridge.hedge(book_pos, lower, 20)
    with pytest.raises(LiveEntryError):
        bridge.hedge(book_pos, further, 40)


def test_coordinator_refuses_unprotected_short():
    from uuid import uuid4

    from sensex_engine.domain import Contract, OptionLeg, Position, Right, Side, TradingMode
    from sensex_engine.live_execution import LiveMultiLegExecutionCoordinator

    positions = FakePositions()
    positions.snaps["long"] = {
        "status": "OPEN",
        "is_fully_open": True,
        "legs": [{"side": "BUY", "remaining_quantity": 20}],
    }
    coordinator = LiveMultiLegExecutionCoordinator(
        orders=None,
        positions=positions,
        submission=None,
        trade_history=None,
        limit_price=lambda a: Decimal("1"),
        live_execution_enabled=True,
    )
    contract = Contract("SENSEXX", 5, NEAR, Decimal("84000"), Right.CE, 20, Decimal("0.05"))
    big = Position(
        uuid4(), S.LEG_IN_CALL, (OptionLeg(contract, Side.SELL, 2),), TradingMode.LIVE, NOW
    )
    with pytest.raises(PermissionError, match="exceeds"):
        coordinator.execute_protected_short(big, protecting_position_id="long")
    with pytest.raises(PermissionError, match="not fully open"):
        coordinator.execute_protected_short(big, protecting_position_id="missing")
    buy = Position(
        uuid4(), S.LEG_IN_CALL, (OptionLeg(contract, Side.BUY, 1),), TradingMode.LIVE, NOW
    )
    with pytest.raises(ValueError, match="SELL"):
        coordinator.execute_protected_short(buy, protecting_position_id="long")


def test_entry_sizing_stop_fraction_only_for_single_long():
    from sensex_engine.entry_sizing import EntrySizingService
    from sensex_engine.risk import RiskEngine

    proposal, _, _ = S.build_bull_put(
        ctx(make_state(), 500000), S.StrategyConfig(), Decimal("9000")
    )
    sizing = EntrySizingService(broker=None, snapshot_provider=lambda: None, risk=RiskEngine())
    assert proposal is not None
    candidate = replace(proposal.to_candidate(1), data_timestamp=NOW)
    with pytest.raises(ValueError, match="single long"):
        sizing.approve(
            candidate,
            max_lots=1,
            allocation_pct=Decimal("1"),
            now=NOW + timedelta(seconds=1),
            stop_fraction=Decimal("0.2"),
        )


def test_leg_in_is_capped_at_one_lot_even_with_large_capital(tmp_path, monkeypatch):
    engine, fk, clock, alerts, p = _enter(tmp_path, monkeypatch, funds=1000000)
    assert p.lots == 1


def test_plus60_variant_holds_lock_to_eod_without_ema_exit():
    plan = S.build_leg_in_call(ctx(bullish_state(), 50000), S.StrategyConfig(), Decimal("2000"))[
        0
    ].exit_plan
    assert plan.stop_loss_pct == 0.25 and plan.hedge_trigger == 0.60
    assert plan.lock_target_width is None and plan.ema_exit is False


def test_one_leg_in_entry_per_direction_per_day(tmp_path, monkeypatch):
    engine, fk, clock, alerts, p = _enter(tmp_path, monkeypatch)
    _tick(engine, fk, clock, 30, SPOT - 300)  # premium stop
    assert engine.book.get(p.id).status == "CLOSED"
    fk.spot = SPOT
    for minutes in (5, 10, 15):  # new bars with the same (stubbed) signal
        clock.now = fk.now = NOW + timedelta(minutes=minutes)
        engine.cycle()
    assert not engine.book.open_positions("paper")
    assert len(engine.book.closed_positions(mode="paper")) == 1
