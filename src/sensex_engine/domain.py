"""Core immutable domain types. Quantities are positive lot counts."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Regime(StrEnum):
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE = "range"
    VOLATILITY_EXPANSION = "volatility_expansion"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class Right(StrEnum):
    CE = "CE"
    PE = "PE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Contract:
    tradingsymbol: str
    instrument_token: int
    expiry: datetime
    strike: Decimal
    right: Right
    lot_size: int
    tick_size: Decimal

    def __post_init__(self) -> None:
        if self.lot_size <= 0 or self.tick_size <= 0 or self.strike < 0:
            raise ValueError("Invalid exchange contract metadata")


@dataclass(frozen=True)
class OptionLeg:
    contract: Contract
    side: Side
    lots: int

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError("Leg lots must be positive")


@dataclass(frozen=True)
class StrategyCandidate:
    strategy_id: str
    regime: Regime
    legs: tuple[OptionLeg, ...]
    max_loss: Decimal
    max_profit: Decimal | None
    net_debit: Decimal
    estimated_costs: Decimal
    data_timestamp: datetime
    is_defined_risk: bool
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.legs:
            raise ValueError("Candidate requires an id and at least one leg")
        if self.max_loss <= 0 or self.estimated_costs < 0:
            raise ValueError("Invalid candidate risk/cost")
        if not self.is_defined_risk:
            raise ValueError("Only defined-risk structures are eligible")


@dataclass(frozen=True)
class PortfolioSnapshot:
    net_liquidation_equity: Decimal
    available_margin: Decimal
    open_max_loss: Decimal
    daily_realized_pnl: Decimal
    daily_unrealized_pnl: Decimal
    weekly_drawdown_pct: Decimal = Decimal("0")
    monthly_drawdown_pct: Decimal = Decimal("0")
    open_positions: int = 0
    entries_today: int = 0
    deposits_withdrawals_today: Decimal = Decimal("0")
    peak_equity: Decimal | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    allowed_max_loss: Decimal = Decimal("0")


@dataclass
class Position:
    position_id: UUID
    strategy_id: str
    legs: tuple[OptionLeg, ...]
    mode: TradingMode
    opened_at: datetime
    status: Literal["OPEN", "CLOSING", "CLOSED", "ERROR"] = "OPEN"

    @classmethod
    def create(cls, strategy_id: str, legs: tuple[OptionLeg, ...], mode: TradingMode) -> "Position":
        return cls(uuid4(), strategy_id, legs, mode, datetime.now().astimezone())
