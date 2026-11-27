"""Kite-backed market data for the strategy engine (read-only).

* BFO contract master refreshed once per session (or when empty).
* 5-minute SENSEX index candles and near-month SENSEX futures candles via
  ``historical_data``; refetched only when a new completed bar is due.
* One batched ``quote`` call per cycle for the index, the future, the option
  strikes around spot for the two nearest non-expiring expiries, and every
  leg of any open position. Respects Kite's 500-instrument quote limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .domain import Contract
from .indicators import IST, Bar
from .kite import KiteReadOnlyAdapter, parse_quote
from .market_data import Quote
from .option_chain import OptionChainSnapshot, build_option_chain

logger = logging.getLogger(__name__)
INDEX_TOKEN = 265
INDEX_SYMBOL = "BSE:SENSEX"
MAX_QUOTE_INSTRUMENTS = 480


@dataclass
class FeedSnapshot:
    as_of: datetime
    spot: Decimal
    spot_time: datetime
    quotes: dict[int, Quote]
    near_expiry: datetime
    far_expiry: datetime | None
    future_token: int | None

    def oi(self) -> dict[int, int]:
        return {token: q.oi for token, q in self.quotes.items()}


@dataclass
class _BarCache:
    bars: list[Bar] = field(default_factory=list)
    fetched_for: datetime | None = None


class MarketFeed:
    def __init__(
        self,
        adapter: KiteReadOnlyAdapter,
        *,
        strike_window_pct: float = 0.035,
        history_days: int = 10,
        chain_quote_age_seconds: int = 15,
        chain_max_spread_pct: Decimal = Decimal("0.25"),
        risk_free_rate: Decimal = Decimal("0.065"),
    ) -> None:
        self.adapter = adapter
        self.client: Any = adapter._client
        self.strike_window_pct = strike_window_pct
        self.history_days = history_days
        self.chain_quote_age_seconds = chain_quote_age_seconds
        self.chain_max_spread_pct = chain_max_spread_pct
        self.risk_free_rate = risk_free_rate
        self.contracts: list[Contract] = []
        self.by_token: dict[int, Contract] = {}
        self.contracts_day: date | None = None
        self._bars: dict[int, _BarCache] = {}

    # --------------------------------------------------------- contracts
    def refresh_contracts(self, now: datetime, *, force: bool = False) -> None:
        today = now.astimezone(IST).date()
        if not force and self.contracts and self.contracts_day == today:
            return
        contracts = self.adapter.instruments()
        if not contracts:
            raise RuntimeError("Kite returned no SENSEX option contracts")
        self.contracts = contracts
        self.by_token = {c.instrument_token: c for c in contracts}
        self.contracts_day = today
        logger.info(
            "SENSEX contract master: %d options, %d futures",
            len(contracts),
            len(self.adapter.futures),
        )

    def expiries(self, now: datetime) -> tuple[datetime, datetime | None]:
        """Nearest expiry strictly after today (expiry-day series excluded) and the next."""
        today = now.astimezone(IST).date()
        dates = sorted(
            {c.expiry for c in self.contracts if c.expiry.astimezone(IST).date() > today}
        )
        if not dates:
            raise RuntimeError("No non-expiring SENSEX expiry is listed")
        return dates[0], (dates[1] if len(dates) > 1 else None)

    def future(self, now: datetime) -> tuple[int, str] | None:
        today = now.astimezone(IST).date()
        for token, symbol, expiry in self.adapter.futures:
            if expiry.astimezone(IST).date() > today:
                return token, symbol
        return None

    # --------------------------------------------------------------- bars
    def bars(self, token: int, now: datetime) -> list[Bar]:
        cache = self._bars.setdefault(token, _BarCache())
        boundary = now.astimezone(IST).replace(second=0, microsecond=0)
        boundary -= timedelta(minutes=boundary.minute % 5)
        expected_last = boundary - timedelta(minutes=5)  # start of the newest completed bar
        if cache.bars and cache.bars[-1].start >= expected_last:
            return cache.bars
        # The broker can publish a bar a few seconds after it closes; retry at most every 40 s.
        if cache.fetched_for is not None and (now - cache.fetched_for).total_seconds() < 40:
            return cache.bars
        start = (now - timedelta(days=self.history_days)).astimezone(IST)
        rows = self.client.historical_data(
            token, start.replace(tzinfo=None), now.astimezone(IST).replace(tzinfo=None), "5minute"
        )
        bars: list[Bar] = []
        for row in rows or []:
            when = row["date"]
            if isinstance(when, str):
                when = datetime.fromisoformat(when)
            when = when.replace(tzinfo=IST) if when.tzinfo is None else when.astimezone(IST)
            try:
                bars.append(
                    Bar(
                        when,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume") or 0),
                    )
                )
            except ValueError:
                continue
        # Never cache the forming bar: a partial bar would otherwise be reused as
        # if complete once its interval ends.
        bars = [bar for bar in bars if bar.start + timedelta(minutes=5) <= now]
        cache.bars, cache.fetched_for = bars, now
        return bars

    # ------------------------------------------------------------- quotes
    def snapshot(
        self, now: datetime, *, extra_tokens: set[int] | None = None, spot_hint: float | None = None
    ) -> FeedSnapshot:
        near, far = self.expiries(now)
        fut = self.future(now)
        spot_raw = self.client.quote([INDEX_SYMBOL]).get(INDEX_SYMBOL)
        spot_quote = _index_quote(spot_raw)
        if spot_quote is None:
            raise RuntimeError("SENSEX index quote unavailable")
        spot, spot_time = spot_quote
        centre = float(spot) if spot else (spot_hint or 0.0)
        window = centre * self.strike_window_pct
        wanted = [
            c
            for c in self.contracts
            if c.expiry in {near, far} and abs(float(c.strike) - centre) <= window
        ]
        wanted.sort(key=lambda c: (c.expiry != near, abs(float(c.strike) - centre)))
        tokens = {c.instrument_token for c in wanted[:MAX_QUOTE_INSTRUMENTS]}
        tokens |= {t for t in (extra_tokens or set()) if t in self.by_token}
        symbols = {f"BFO:{self.by_token[t].tradingsymbol}": t for t in tokens}
        if fut:
            symbols[f"BFO:{fut[1]}"] = fut[0]
        raw = self.client.quote(list(symbols)) if symbols else {}
        quotes: dict[int, Quote] = {}
        for symbol, token in symbols.items():
            quote = parse_quote(token, raw.get(symbol))
            if quote is not None:
                quotes[token] = quote
        return FeedSnapshot(now, spot, spot_time, quotes, near, far, fut[0] if fut else None)

    def chain(self, snap: FeedSnapshot, expiry: datetime | None) -> OptionChainSnapshot | None:
        if expiry is None:
            return None
        contracts = [
            c for c in self.contracts if c.expiry == expiry and c.instrument_token in snap.quotes
        ]
        if not contracts:
            return None
        return build_option_chain(
            contracts,
            snap.quotes,
            underlying_price=snap.spot,
            underlying_timestamp=snap.spot_time,
            as_of=snap.as_of,
            risk_free_rate=self.risk_free_rate,
            max_quote_age_seconds=self.chain_quote_age_seconds,
            max_spread_pct=self.chain_max_spread_pct,
        )


def _index_quote(raw: dict[str, Any] | None) -> tuple[Decimal, datetime] | None:
    if not raw or not raw.get("last_price"):
        return None
    stamp = raw.get("timestamp") or raw.get("last_trade_time")
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return Decimal(str(raw["last_price"])), stamp


def atm_iv(chain: OptionChainSnapshot | None) -> tuple[float | None, float | None]:
    """(average ATM CE/PE IV, ATM straddle mid) from a validated chain."""
    if chain is None or not chain.analytics:
        return None, None
    spot = chain.underlying_price
    strikes = {}
    for a in chain.analytics:
        strikes.setdefault(a.contract.strike, {})[a.contract.right.value] = a
    paired = [k for k, v in strikes.items() if "CE" in v and "PE" in v]
    if not paired:
        return None, None
    k = min(paired, key=lambda s: abs(s - spot))
    ce, pe = strikes[k]["CE"], strikes[k]["PE"]
    iv = (float(ce.implied_volatility) + float(pe.implied_volatility)) / 2
    return iv, float(ce.mid_price + pe.mid_price)
