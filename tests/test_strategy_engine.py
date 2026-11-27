from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from engine_fakes import (
    FAR,
    NEAR,
    NOW,
    SPOT,
    FakeKite,
    bullish_state,
    chain,
    history,
    make_state,
)

from sensex_engine import strategies as S
from sensex_engine.arbitration import arbitrate
from sensex_engine.book import EngineBook
from sensex_engine.capital_policy import risk_policy, tier_for
from sensex_engine.charges import leg_charges, round_trip_estimate
from sensex_engine.domain import Regime
from sensex_engine.engine import Engine, EngineConfig, Notifier
from sensex_engine.exits import evaluate_exit
from sensex_engine.indicators import (
    IST,
    Bar,
    ema,
    market_structure,
    session_vwap,
    wilder_adx,
    wilder_atr,
    wilder_rsi,
)
from sensex_engine.kite import KiteReadOnlyAdapter
from sensex_engine.market_feed import MarketFeed
from sensex_engine.market_state import build_market_state


def ctx(state, *, far=True, now=NOW):
    near_chain = chain(NEAR)
    far_chain = chain(FAR) if far else None
    return S.MarketContext(state=state, near=near_chain, far=far_chain, oi={}, now=now)


# ------------------------------------------------------------------ indicators
def test_indicators_match_closed_form_expectations():
    flat = [100.0] * 40
    assert ema(flat, 9)[-1] == pytest.approx(100.0)
    assert ema([1.0, 2.0], 3) == [None, None]
    rising = [100.0 + i for i in range(40)]
    assert wilder_rsi(rising)[-1] == 100.0
    start = datetime(2026, 11, 24, 9, 15, tzinfo=IST)
    bars = [
        Bar(start + timedelta(minutes=5 * i), 100 + i, 101 + i + 0.5, 99 + i, 100.5 + i, 10)
        for i in range(60)
    ]
    assert wilder_atr(bars)[-1] == pytest.approx(2.5, rel=0.05)
    dm = wilder_adx(bars)
    assert dm.adx[-1] > 50 and dm.plus_di[-1] > dm.minus_di[-1]


def test_vwap_resets_each_session_and_is_none_without_volume():
    a = datetime(2026, 11, 23, 15, 25, tzinfo=IST)
    b = datetime(2026, 11, 24, 9, 15, tzinfo=IST)
    bars = [
        Bar(a, 100, 101, 99, 100, 10),
        Bar(b, 200, 201, 199, 200, 10),
        Bar(b + timedelta(minutes=5), 200, 201, 199, 200, 0),
    ]
    vw = session_vwap(bars)
    assert vw[0] == pytest.approx(100) and vw[1] == pytest.approx(200)
    assert session_vwap([Bar(a, 100, 101, 99, 100, 0)]) == [None]


def test_market_structure_detects_hh_hl_and_bullish_bos():
    start = datetime(2026, 11, 24, 9, 15, tzinfo=IST)
    path = [100, 104, 108, 104, 101, 105, 110, 114, 110, 106, 109, 113, 118, 121, 123]
    bars = [Bar(start + timedelta(minutes=5 * i), p, p + 1, p - 1, p) for i, p in enumerate(path)]
    st = market_structure(bars)
    assert st.higher_high and st.higher_low and st.hh_hl and st.bullish_bos
    assert not st.ll_lh


def test_build_market_state_from_bars_uses_futures_for_vwap():
    fk = FakeKite()
    state = build_market_state(fk.index_bars, now=NOW, spot=SPOT, future_bars=fk.future_bars)
    assert state.vwap is not None and state.volume == 1000.0
    assert state.opening_range_high is not None and state.prev_day_high is not None
    no_volume = build_market_state(fk.index_bars, now=NOW, spot=SPOT, future_bars=None)
    assert no_volume.vwap is None and not no_volume.vwap_available
    with pytest.raises(ValueError):
        build_market_state(fk.index_bars[:50], now=NOW, spot=SPOT)
    with pytest.raises(ValueError, match="stale"):
        build_market_state(fk.index_bars, now=NOW + timedelta(hours=2), spot=SPOT)


# ------------------------------------------------------------- capital / costs
def test_capital_tiers_scale_risk_with_equity():
    small = risk_policy(equity=Decimal("40000"), peak_equity=Decimal("40000"))
    assert small.tier.name == "micro" and small.max_trade_risk == Decimal("1600.00")
    big = risk_policy(equity=Decimal("1500000"), peak_equity=Decimal("1500000"))
    assert big.tier.name == "standard" and big.max_trade_risk == Decimal("15000.00")
    assert tier_for(Decimal("250000")).name == "small"
    # Micro tier bands are scaled 2x: 7% drawdown -> 25% cut, 21% -> suspended.
    cut = risk_policy(equity=Decimal("93000"), peak_equity=Decimal("100000"))
    assert cut.multiplier == Decimal("0.75")
    halted = risk_policy(equity=Decimal("79000"), peak_equity=Decimal("100000"))
    assert not halted.entries_allowed
    with pytest.raises(ValueError):
        risk_policy(equity=Decimal("0"), peak_equity=Decimal("1"))


def test_charges_include_stt_on_sell_and_stamp_on_buy():
    sell = leg_charges(side="SELL", price=Decimal("100"), quantity=20)
    buy = leg_charges(side="BUY", price=Decimal("100"), quantity=20)
    assert sell.stt == Decimal("2.00") and buy.stt == 0
    assert buy.stamp > 0 and sell.stamp == 0
    assert sell.brokerage == Decimal("20.00")
    assert round_trip_estimate([("BUY", Decimal("100"), 20)]) == buy.total + sell.total


# ------------------------------------------------------------------ strategies
def test_bull_call_builder_selects_delta_band_strikes_within_budget():
    state = bullish_state()
    proposal, why, _ = S.build_bull_call(ctx(state), S.StrategyConfig(), Decimal("8000"))
    assert proposal is not None, why
    (long_leg, ls), (short_leg, ss) = proposal.legs
    assert ls.value == "BUY" and ss.value == "SELL"
    assert 0.50 <= float(long_leg.delta) <= 0.70 and 0.20 <= float(short_leg.delta) <= 0.40
    assert short_leg.contract.strike > long_leg.contract.strike
    assert proposal.risk_per_lot <= Decimal("8000")
    assert proposal.max_profit_per_lot / proposal.max_loss_per_lot >= Decimal("1.1")
    assert proposal.prices[0] == long_leg.quote.ask and proposal.prices[1] == short_leg.quote.bid
    assert set(proposal.components) == set("PLVGORC") and 0 < proposal.score <= 100


def test_builder_rejects_when_one_lot_exceeds_capital_budget():
    proposal, why, rejected = S.build_bull_call(
        ctx(bullish_state()), S.StrategyConfig(), Decimal("200")
    )
    assert proposal is None and rejected > 0 and "budget" in why


def test_bull_call_rejects_resistance_inside_reward_distance():
    state = bullish_state(prev_day_high=SPOT + 60)
    proposal, why, _ = S.build_bull_call(ctx(state), S.StrategyConfig(), Decimal("9000"))
    assert proposal is None and "resistance" in why


def test_credit_spread_short_strike_must_clear_support():
    state = make_state(structure=replace(make_state().structure, last_lows=(SPOT - 800, SPOT - 50)))
    proposal, why, _ = S.build_bull_put(ctx(state), S.StrategyConfig(), Decimal("6000"))
    if proposal is not None:
        (lo, _), (sh, _) = proposal.legs
        assert float(sh.contract.strike) < SPOT - 50
        assert lo.contract.strike < sh.contract.strike
        assert proposal.net_debit_per_lot < 0
    else:
        assert why


def test_iron_condor_shorts_outside_range_and_expected_move():
    state = make_state(range_low=SPOT - 150, range_high=SPOT + 150)
    proposal, why, _ = S.build_iron_condor(
        ctx(state), S.StrategyConfig(min_credit_to_width=0.05), Decimal("8000")
    )
    assert proposal is not None, why
    em = state.expected_move()
    shorts = [a for a, side in proposal.legs if side.value == "SELL"]
    longs = [a for a, side in proposal.legs if side.value == "BUY"]
    assert len(shorts) == 2 and len(longs) == 2
    for a in shorts:
        if a.contract.right.value == "PE":
            assert float(a.contract.strike) < SPOT - em
        else:
            assert float(a.contract.strike) > SPOT + em
    assert proposal.net_debit_per_lot < 0 and proposal.max_profit_per_lot is not None


def test_long_iron_butterfly_is_debit_with_capped_loss():
    proposal, why, _ = S.build_long_iron_butterfly(
        ctx(make_state()), S.StrategyConfig(min_long_ib_rr=0.2, min_score=0), Decimal("20000")
    )
    assert proposal is not None, why
    assert proposal.net_debit_per_lot > 0
    assert proposal.max_loss_per_lot == pytest.approx(proposal.net_debit_per_lot, rel=1e-6)


def test_calendar_uses_two_expiries_and_bounds_loss_by_debit():
    proposal, why, _ = S.build_calendar(ctx(make_state()), S.StrategyConfig(), Decimal("20000"))
    assert proposal is not None, why
    assert proposal.multi_expiry
    expiries = {a.contract.expiry for a, _ in proposal.legs}
    assert expiries == {NEAR, FAR}
    assert proposal.max_loss_per_lot == proposal.net_debit_per_lot > 0
    missing, why, _ = S.build_calendar(
        ctx(make_state(), far=False), S.StrategyConfig(), Decimal("20000")
    )
    assert missing is None and "back-month" in why


def test_setup_checks_report_every_failed_rule():
    checks = S.setup_bull_call(make_state(), NOW)
    failed = {c.name for c in checks if not c.passed}
    assert "HH-HL structure" in failed and "RSI > 55" in failed
    passed = S.setup_bull_call(bullish_state(), NOW)
    assert all(c.passed for c in passed), [c for c in passed if not c.passed]
    no_vwap = S.setup_bull_call(bullish_state(vwap=None, future_close=None), NOW)
    assert any(c.name == "VWAP" and not c.passed for c in no_vwap)
    late = S.setup_bull_call(bullish_state(), NOW.replace(hour=14, minute=45))
    assert any(c.name == "session window" and not c.passed for c in late)


def test_evaluate_all_returns_priority_ordered_diagnostics():
    evaluations = S.evaluate_all(ctx(bullish_state()), risk_budget=Decimal("8000"))
    assert [e.strategy_id for e in evaluations] == list(S.PRIORITY)
    assert evaluations[0].eligible, evaluations[0].summary()
    assert all(e.summary() for e in evaluations)


# ----------------------------------------------------------------- arbitration
def test_arbitration_priority_and_portfolio_conflicts():
    evaluations = S.evaluate_all(ctx(bullish_state()), risk_budget=Decimal("8000"))
    chosen = arbitrate(evaluations, open_strategies=[], max_concurrent=3)
    assert chosen.selected is not None and chosen.selected.strategy_id == S.BULL_CALL
    blocked = arbitrate(evaluations, open_strategies=[S.BEAR_PUT], max_concurrent=3)
    assert blocked.selected is None and "conflicts" in blocked.considered[0]
    full = arbitrate(evaluations, open_strategies=[S.IRON_CONDOR, S.CALENDAR], max_concurrent=2)
    assert full.selected is None and "max concurrent" in full.reason


# ----------------------------------------------------------------------- exits
def _book_position(tmp_path, **kw):
    from sensex_engine.book import BookLeg, BookPosition

    base = dict(
        id="p1",
        mode="paper",
        strategy_id=S.BULL_PUT,
        status="OPEN",
        opened_at=NOW.isoformat(),
        session_day=NOW.date().isoformat(),
        lots=1,
        lot_size=20,
        entry_net=Decimal("-1000"),
        max_loss=Decimal("3000"),
        max_profit=Decimal("1000"),
        entry_costs=Decimal("50"),
        legs=[
            BookLeg(1, "A", "PE", 82000, NEAR.isoformat(), "BUY", 20, 20.0, 0.14),
            BookLeg(2, "B", "PE", 82200, NEAR.isoformat(), "SELL", 20, 70.0, 0.14),
        ],
        exit_plan=S.ExitPlan(
            0.5, "max_profit", 1.0, 0.45, 82500.0, None, alignment="bullish"
        ).__dict__.copy(),
    )
    base.update(kw)
    return BookPosition(**base)


def test_exit_rules_priority_target_stop_cutoff_invalidation(tmp_path):
    p = _book_position(tmp_path)
    state = make_state(close=SPOT)
    assert (
        evaluate_exit(p, pnl=600, short_deltas={}, state=state, new_bar=True, now=NOW).category
        == "target"
    )
    assert (
        evaluate_exit(p, pnl=-1001, short_deltas={}, state=state, new_bar=True, now=NOW).category
        == "stop"
    )
    assert (
        evaluate_exit(
            p,
            pnl=0,
            short_deltas={},
            state=state,
            new_bar=True,
            now=NOW.replace(hour=15, minute=11),
        ).category
        == "cutoff"
    )
    broken = evaluate_exit(
        p, pnl=100, short_deltas={}, state=make_state(close=82400), new_bar=True, now=NOW
    )
    assert broken.category == "invalidation"
    delta = evaluate_exit(p, pnl=0, short_deltas={2: -0.5}, state=state, new_bar=False, now=NOW)
    assert delta.category == "invalidation"
    flip = evaluate_exit(
        p, pnl=0, short_deltas={}, state=make_state(regime=Regime.BEAR_TREND), new_bar=True, now=NOW
    )
    assert "regime" in flip.reason
    assert evaluate_exit(p, pnl=10, short_deltas={}, state=state, new_bar=True, now=NOW) is None


# ---------------------------------------------------------------------- engine
class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _engine(tmp_path, fk, clock, **cfg):
    adapter = KiteReadOnlyAdapter(fk)
    feed = MarketFeed(adapter)
    book = EngineBook(tmp_path / "engine.db")
    alerts = []
    engine = Engine(
        feed=feed,
        book=book,
        notifier=Notifier(alerts.append),
        config=EngineConfig(**cfg),
        clock=clock,
    )
    return engine, book, alerts


def test_engine_paper_cycle_enters_sizes_and_squares_off(tmp_path, monkeypatch):
    monkeypatch.setitem(S.SETUPS, S.BULL_CALL, lambda state, now: [])
    clock = Clock(NOW)
    fk = FakeKite(funds=400000)
    engine, book, alerts = _engine(tmp_path, fk, clock, enabled_strategies=frozenset({S.BULL_CALL}))
    engine.cycle()
    assert engine.last_error == "", engine.last_error
    positions = book.open_positions("paper")
    assert len(positions) == 1, engine.last_arbitration
    p = positions[0]
    assert p.strategy_id == S.BULL_CALL and p.lots >= 1
    policy = risk_policy(equity=Decimal("400000"), peak_equity=Decimal("400000"))
    assert p.max_loss + p.entry_costs <= policy.max_trade_risk
    assert book.get_state("paper_base_capital") == "400000"
    assert any("ENTRY" in a for a in alerts)
    # Same bar again: no duplicate entry.
    engine.cycle()
    assert len(book.open_positions("paper")) == 1
    # Square-off.
    later = NOW.replace(hour=15, minute=11)
    clock.now = later
    fk.now = later
    fk.index_bars = history(later)
    fk.future_bars = [
        replace(b, close=b.close + 40, high=b.high + 40, low=b.low + 40, open=b.open + 40)
        for b in fk.index_bars
    ]
    engine.cycle()
    assert not book.open_positions("paper")
    closed = book.closed_positions(mode="paper")
    assert closed and closed[0].exit_reason.startswith("intraday square-off")
    assert closed[0].realized_pnl is not None and closed[0].exit_costs > 0
    assert any("EXIT" in a for a in alerts)


def test_engine_does_not_trade_when_budget_cannot_fund_a_lot(tmp_path, monkeypatch):
    monkeypatch.setitem(S.SETUPS, S.BULL_CALL, lambda state, now: [])
    engine, book, _ = _engine(
        tmp_path, FakeKite(funds=2000), Clock(NOW), enabled_strategies=frozenset({S.BULL_CALL})
    )
    engine.cycle()
    assert not book.open_positions()
    assert "budget" in engine.last_evaluations[0].summary()


def test_engine_respects_operator_pause_and_stale_market(tmp_path, monkeypatch):
    monkeypatch.setitem(S.SETUPS, S.BULL_CALL, lambda state, now: [])
    fk = FakeKite(funds=400000)
    engine, book, _ = _engine(tmp_path, fk, Clock(NOW), enabled_strategies=frozenset({S.BULL_CALL}))
    engine.user_paused = True
    engine.cycle()
    assert not book.open_positions() and "paused" in engine.last_arbitration
    stale = FakeKite(funds=400000)
    stale.now = NOW - timedelta(minutes=10)
    engine2, book2, _ = _engine(
        tmp_path / "b", stale, Clock(NOW), enabled_strategies=frozenset({S.BULL_CALL})
    )
    engine2.cycle()
    assert not book2.open_positions() and "old" in engine2.last_error


def test_engine_daily_loss_stop_closes_everything_and_halts(tmp_path, monkeypatch):
    monkeypatch.setitem(S.SETUPS, S.BULL_CALL, lambda state, now: [])
    clock = Clock(NOW)
    fk = FakeKite(funds=400000)
    engine, book, alerts = _engine(tmp_path, fk, clock, enabled_strategies=frozenset({S.BULL_CALL}))
    engine.cycle()
    assert book.open_positions("paper")
    # Crash the market 3% within the session: spread loses most of its value.
    clock.now = fk.now = NOW + timedelta(seconds=30)
    fk.spot = SPOT * 0.97
    book.set_state("paper_base_capital", "400000")
    row = book.equity_day("paper", NOW.date())
    # Tighten the day by pretending a large realised loss already happened.
    with book._conn() as conn:
        conn.execute("UPDATE eng_equity SET start_equity='60000' WHERE mode='paper'")
    engine.cycle()
    assert row is not None
    assert not book.open_positions("paper")
    assert book.equity_day("paper", NOW.date())["halted"] == 1
    assert any("Daily loss stop" in a for a in alerts)


def test_engine_outside_session_is_idle(tmp_path):
    fk = FakeKite()
    engine, _, _ = _engine(tmp_path, fk, Clock(NOW.replace(hour=8)))
    engine.cycle()
    assert fk.quote_calls == 0
    saturday = NOW + timedelta(days=4)
    engine2, _, _ = _engine(tmp_path / "s", fk, Clock(saturday))
    engine2.cycle()
    assert fk.quote_calls == 0


def test_status_and_report_texts(tmp_path, monkeypatch):
    monkeypatch.setitem(S.SETUPS, S.BULL_CALL, lambda state, now: [])
    engine, _, _ = _engine(
        tmp_path, FakeKite(funds=400000), Clock(NOW), enabled_strategies=frozenset({S.BULL_CALL})
    )
    assert "No strategy evaluation" in engine.strategies_text()
    engine.cycle()
    assert "SENSEX" in engine.status_text() and "Capital tier: medium" in engine.status_text()
    assert "1. Bull call spread: ELIGIBLE" in engine.strategies_text()
    capital = engine.capital_text()
    for label in ("PAPER account", "  Equity: Rs ", "  Start of day: ", "  Risk per trade: ",
                  "  Daily loss stop: ", "  Capital tier: medium"):
        assert label in capital, capital
    risk = engine.risk_text()
    assert "Risk and limits" in risk and "  Week drawdown: " in risk
    assert all(len(line) < 90 for line in capital.splitlines() + risk.splitlines())
    status = engine.status_text()
    assert "Mode: PAPER" in status and "  Regime: " in status
    assert "PAPER" in engine.positions_text()
    assert "Equity:" in engine.capital_text()
    assert "PAPER" in engine.pnl_text()
    pid = engine.book.open_positions()[0].id
    assert engine.close_one(pid).startswith("Closed")
    assert "No open engine position" in engine.close_one(pid)


def test_book_equity_rows_are_idempotent_and_carry_peak(tmp_path):
    book = EngineBook(tmp_path / "e.db")
    d1, d2 = NOW.date() - timedelta(days=1), NOW.date()
    book.start_day("paper", d1, Decimal("50000"))
    book.update_peak("paper", d1, Decimal("52000"))
    row = book.start_day("paper", d2, Decimal("51000"))
    assert Decimal(row["peak_equity"]) == Decimal("52000")
    assert book.start_day("paper", d2, Decimal("1"))["start_equity"] == "51000"
    assert book.period_reference("paper", d1) == Decimal("52000")
    assert book.health_check()


def test_square_off_time_constant():
    assert EngineConfig().square_off == time(15, 10)
    assert datetime.now(UTC).tzinfo is not None
