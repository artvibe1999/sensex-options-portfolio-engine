"""Market snapshot quality gates; do not infer option-chain data from stale quotes."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Quote:
    instrument_token: int
    timestamp: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    bid_quantity: int
    ask_quantity: int
    volume: int

    def __post_init__(self) -> None:
        if self.instrument_token <= 0:
            raise ValueError("Instrument token must be positive")
        if min(self.last_price, self.bid, self.ask) < 0:
            raise ValueError("Prices cannot be negative")
        if self.ask and self.bid > self.ask:
            raise ValueError("Crossed quote")
        if min(self.bid_quantity, self.ask_quantity, self.volume) < 0:
            raise ValueError("Quote quantities cannot be negative")


@dataclass(frozen=True)
class QuoteQuality:
    valid: bool
    reason: str
    spread_pct: Decimal


def validate_quote(
    quote: Quote,
    *,
    now: datetime,
    max_age_seconds: int = 5,
    max_spread_pct: Decimal = Decimal("0.03"),
    min_top_depth: int = 1,
    min_volume: int = 1,
) -> QuoteQuality:
    if now.tzinfo is None or quote.timestamp.tzinfo is None:
        return QuoteQuality(False, "Timezone-aware timestamps are required", Decimal("0"))
    age = (now - quote.timestamp).total_seconds()
    if age < -1 or age > max_age_seconds:
        return QuoteQuality(False, "Quote is stale or timestamp is in the future", Decimal("0"))
    if quote.last_price <= 0 or quote.bid <= 0 or quote.ask <= 0:
        return QuoteQuality(False, "Missing or invalid tradable prices", Decimal("0"))
    mid = (quote.bid + quote.ask) / Decimal("2")
    spread_pct = (quote.ask - quote.bid) / mid if mid > 0 else Decimal("1")
    if spread_pct > max_spread_pct:
        return QuoteQuality(False, "Bid-ask spread exceeds liquidity threshold", spread_pct)
    if min(quote.bid_quantity, quote.ask_quantity) < min_top_depth:
        return QuoteQuality(False, "Insufficient top-of-book depth", spread_pct)
    if quote.volume < min_volume:
        return QuoteQuality(False, "Insufficient traded volume", spread_pct)
    return QuoteQuality(True, "Quote passes freshness and liquidity checks", spread_pct)
