"""Deterministic fakes for strategy-engine tests (no network, no credentials)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import sin

from sensex_engine.domain import Contract, Regime, Right
from sensex_engine.indicators import IST, Bar, StructureState
from sensex_engine.market_data import Quote
from sensex_engine.market_state import MarketState
from sensex_engine.option_chain import build_option_chain
from sensex_engine.scenario import bs_price, expiry_close, years_between

NOW = datetime(2026, 11, 24, 11, 0, 5, tzinfo=IST)  # Tuesday
NEAR = datetime(2026, 11, 26, tzinfo=IST)  # Thursday expiry
FAR = datetime(2026, 12, 3, tzinfo=IST)
SPOT = 83000.0
LOT = 20
TICK = Decimal("0.05")


def tick(value: float) -> Decimal:
    return (Decimal(str(value)) / TICK).to_integral_value(rounding=ROUND_HALF_UP) * TICK


def contracts(expiries=(NEAR, FAR), lo=79000, hi=87000, step=100) -> list[Contract]:
    out, token = [], 1000
    for expiry in expiries:
        for strike in range(lo, hi + step, step):
            for right in (Right.CE, Right.PE):
                token += 1
                tag = expiry.strftime("%y%b").upper()
                out.append(
                    Contract(
                        f"SENSEX{tag}{strike}{right.value}",
                        token,
                        expiry,
                        Decimal(strike),
                        right,
                        LOT,
                        TICK,
                    )
                )
    return out


def theo(contract: Contract, spot: float, now: datetime, iv: float) -> float:
    years = years_between(now, expiry_close(contract.expiry))
    return bs_price(spot, float(contract.strike), years, 0.065, iv, contract.right.value)


def quotes_for(
    cs: list[Contract], *, spot=SPOT, now=NOW, iv_near=0.14, iv_far=0.13, spread_pct=0.01, depth=600
) -> dict[int, Quote]:
    out = {}
    for c in cs:
        iv = iv_near if c.expiry == NEAR else iv_far
        mid = max(theo(c, spot, now, iv), 0.2)
        half = max(mid * spread_pct / 2, 0.05)
        bid, ask = tick(max(mid - half, 0.05)), tick(mid + half)
        if ask <= bid:
            ask = bid + TICK
        out[c.instrument_token] = Quote(
            c.instrument_token,
            now,
            Decimal(str(round(mid, 2))),
            bid,
            ask,
            depth,
            depth,
            5000,
            oi=1000,
        )
    return out


def chain(expiry=NEAR, **kw):
    cs = [c for c in contracts() if c.expiry == expiry]
    q = quotes_for(cs, **kw)
    spot = kw.get("spot", SPOT)
    now = kw.get("now", NOW)
    return build_option_chain(
        cs,
        q,
        underlying_price=Decimal(str(spot)),
        underlying_timestamp=now,
        as_of=now,
        risk_free_rate=Decimal("0.065"),
        max_quote_age_seconds=15,
        max_spread_pct=Decimal("0.25"),
    )


def make_state(**overrides) -> MarketState:
    """Neutral, fully populated market state; tests override what they need."""
    base = MarketState(
        as_of=NOW,
        bar_time=NOW - timedelta(minutes=5, seconds=5),
        spot=SPOT,
        close=SPOT,
        prev_close=SPOT - 10,
        prev_high=SPOT + 20,
        prev_low=SPOT - 30,
        ema9=SPOT,
        ema20=SPOT,
        ema21=SPOT,
        ema50=SPOT,
        ema200=SPOT,
        ema9_slope=0.0,
        ema21_slope=0.0,
        adx=15.0,
        adx_prev=15.0,
        adx_min_recent=14.0,
        plus_di=20.0,
        minus_di=20.0,
        rsi=50.0,
        macd_hist=0.0,
        macd_hist_prev=0.0,
        atr=40.0,
        atr_prev=42.0,
        atr_median=45.0,
        bandwidth=0.002,
        bandwidth_prev=0.0021,
        bandwidth_rank=25.0,
        bandwidth_min_recent_rank=20.0,
        bandwidth_mean=0.0025,
        structure=StructureState((), (), False, False, False, False, False, False, False, False),
        opening_range_high=SPOT + 150,
        opening_range_low=SPOT - 150,
        prev_day_high=SPOT + 600,
        prev_day_low=SPOT - 600,
        prev_day_close=SPOT,
        prev_week_high=SPOT + 1200,
        prev_week_low=SPOT - 1200,
        session_high=SPOT + 200,
        session_low=SPOT - 200,
        session_bars=21,
        range_high=SPOT + 200,
        range_low=SPOT - 200,
        range_high_tests=2,
        range_low_tests=2,
        ema_crossings=3,
        rv_annual=0.10,
        rv_short=0.09,
        vwap=SPOT + 5,
        future_close=SPOT + 10,
        volume=1200.0,
        volume_avg=1000.0,
        vwap_crossings=3,
        atm_iv_near=0.14,
        atm_iv_far=0.13,
        straddle_near=900.0,
        days_to_near_expiry=2.2,
    )
    return replace(base, **overrides)


def bullish_state(**overrides) -> MarketState:
    s = make_state(
        close=SPOT,
        prev_close=SPOT - 120,
        ema9=SPOT - 60,
        ema21=SPOT - 150,
        ema50=SPOT - 300,
        ema20=SPOT - 140,
        adx=28.0,
        adx_prev=24.0,
        plus_di=32.0,
        minus_di=14.0,
        rsi=63.0,
        macd_hist=12.0,
        atr=60.0,
        atr_median=50.0,
        vwap=SPOT - 120,
        future_close=SPOT + 20,
        structure=StructureState(
            (SPOT - 400, SPOT - 60),
            (SPOT - 700, SPOT - 300),
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
        ),
        prev_day_high=SPOT - 200,
        prev_week_high=SPOT - 100,
        regime=Regime.BULL_TREND,
        bias="bullish",
    )
    return replace(s, **overrides)


# ------------------------------------------------------------- bars / Kite
def session_bars(day: date, start_price: float, drift: float, *, volume=1000.0, wiggle=15.0):
    bars, price = [], start_price
    t = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=15)
    for i in range(75):
        o = price
        c = o + drift + wiggle * sin(i / 2.0)
        bars.append(Bar(t + timedelta(minutes=5 * i), o, max(o, c) + 10, min(o, c) - 10, c, volume))
        price = c
    return bars


def history(now: datetime = NOW, *, drift=2.0, volume=1000.0) -> list[Bar]:
    days = [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23), now.date()]
    bars, price = [], SPOT - 600
    for day in days:
        day_bars = session_bars(day, price, drift, volume=volume)
        bars += day_bars
        price = day_bars[-1].close
    return [b for b in bars if b.start + timedelta(minutes=5) <= now]


class FakeKite:
    """Minimal KiteConnect stand-in for read-only adapter + engine tests."""

    def __init__(self, *, now=NOW, spot=SPOT, funds=50000.0, index_bars=None, future_bars=None):
        self.now = now
        self.spot = spot
        self.funds = funds
        self.contracts = contracts()
        self.index_bars = index_bars if index_bars is not None else history(now)
        self.future_bars = (
            future_bars
            if future_bars is not None
            else [
                replace(b, open=b.open + 40, high=b.high + 40, low=b.low + 40, close=b.close + 40)
                for b in self.index_bars
            ]
        )
        self.access_token = None
        self.quote_calls = 0
        self.historical_calls = 0

    def set_access_token(self, token):
        self.access_token = token

    def profile(self):
        return {"user_id": "TEST"}

    def instruments(self, exchange):
        assert exchange == "BFO"
        rows = [
            {
                "instrument_token": c.instrument_token,
                "tradingsymbol": c.tradingsymbol,
                "name": "SENSEX",
                "expiry": c.expiry.date(),
                "strike": float(c.strike),
                "instrument_type": c.right.value,
                "lot_size": c.lot_size,
                "tick_size": float(c.tick_size),
            }
            for c in self.contracts
        ]
        rows.append(
            {
                "instrument_token": 999001,
                "tradingsymbol": "SENSEX26DECFUT",
                "name": "SENSEX",
                "expiry": date(2026, 12, 24),
                "strike": 0.0,
                "instrument_type": "FUT",
                "lot_size": LOT,
                "tick_size": 0.05,
            }
        )
        return rows

    def historical_data(self, token, start, end, interval):
        self.historical_calls += 1
        bars = self.index_bars if token == 265 else self.future_bars
        return [
            {
                "date": b.start,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": 0 if token == 265 else b.volume,
            }
            for b in bars
        ]

    def quote(self, symbols):
        self.quote_calls += 1
        by_symbol = {f"BFO:{c.tradingsymbol}": c for c in self.contracts}
        qs = quotes_for(list(by_symbol.values()), spot=self.spot, now=self.now)
        out = {}
        for sym in symbols:
            if sym == "BSE:SENSEX":
                out[sym] = {"last_price": self.spot, "timestamp": self.now.replace(tzinfo=None)}
            elif sym == "BFO:SENSEX26DECFUT":
                out[sym] = {
                    "last_price": self.spot + 40,
                    "timestamp": self.now.replace(tzinfo=None),
                    "volume": 100000,
                    "depth": {
                        "buy": [{"price": self.spot + 39.95, "quantity": 100}],
                        "sell": [{"price": self.spot + 40.05, "quantity": 100}],
                    },
                }
            elif sym in by_symbol:
                q = qs[by_symbol[sym].instrument_token]
                out[sym] = {
                    "last_price": float(q.last_price),
                    "timestamp": self.now.replace(tzinfo=None),
                    "volume": q.volume,
                    "oi": q.oi,
                    "depth": {
                        "buy": [{"price": float(q.bid), "quantity": q.bid_quantity}],
                        "sell": [{"price": float(q.ask), "quantity": q.ask_quantity}],
                    },
                }
        return out

    def margins(self, segment):
        return {
            "enabled": True,
            "net": self.funds,
            "available": {
                "opening_balance": self.funds,
                "intraday_payin": 0,
                "cash": self.funds,
                "live_balance": self.funds,
            },
            "utilised": {"debits": 0},
        }

    def basket_order_margins(self, orders, consider_positions=True):
        total = 0.0
        for o in orders:
            total += o["price"] * o["quantity"] * (1 if o["transaction_type"] == "BUY" else 0.0)
        total = max(total, 5000.0)
        return {"initial": {"total": total}, "final": {"total": total}}

    def positions(self):
        return {"net": []}

    def orders(self):
        return []
