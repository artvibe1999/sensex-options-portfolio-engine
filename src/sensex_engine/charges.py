"""Versioned transaction-cost model for SENSEX index options (Zerodha, BSE).

Rates are published broker/exchange schedules at the time of writing and must
be reviewed when they change; live P&L should later be reconciled against the
broker contract note. Values are per executed order leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CHARGES_VERSION = "zerodha-bse-options-2026-11"
PAISE = Decimal("0.01")


@dataclass(frozen=True)
class ChargeSchedule:
    brokerage_per_order: Decimal = Decimal("20")
    stt_sell_pct: Decimal = Decimal("0.001")  # 0.1% of sell premium
    exchange_txn_pct: Decimal = Decimal("0.000325")  # BSE SENSEX options, of premium
    sebi_per_crore: Decimal = Decimal("10")
    stamp_buy_pct: Decimal = Decimal("0.00003")  # 0.003% of buy premium
    gst_pct: Decimal = Decimal("0.18")
    version: str = CHARGES_VERSION


@dataclass(frozen=True)
class LegCharges:
    brokerage: Decimal
    stt: Decimal
    exchange: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst


def leg_charges(
    *,
    side: str,
    price: Decimal,
    quantity: int,
    schedule: ChargeSchedule = ChargeSchedule(),
) -> LegCharges:
    if price < 0 or quantity <= 0 or side not in {"BUY", "SELL"}:
        raise ValueError("Invalid leg for charge calculation")
    turnover = price * quantity
    brokerage = min(schedule.brokerage_per_order, turnover * Decimal("0.03"))
    stt = turnover * schedule.stt_sell_pct if side == "SELL" else Decimal("0")
    exchange = turnover * schedule.exchange_txn_pct
    sebi = turnover * schedule.sebi_per_crore / Decimal("10000000")
    stamp = turnover * schedule.stamp_buy_pct if side == "BUY" else Decimal("0")
    gst = (brokerage + exchange + sebi) * schedule.gst_pct

    def r(value: Decimal) -> Decimal:
        return value.quantize(PAISE, rounding=ROUND_HALF_UP)

    return LegCharges(r(brokerage), r(stt), r(exchange), r(sebi), r(stamp), r(gst))


def round_trip_estimate(
    legs: list[tuple[str, Decimal, int]],
    schedule: ChargeSchedule = ChargeSchedule(),
) -> Decimal:
    """Entry + exit charges for (side, entry_price, quantity) legs.

    Exit is assumed at the same premium on the opposite side, a neutral
    estimate used for candidate approval; actual exit charges are booked from
    the actual exit fills.
    """
    total = Decimal("0")
    for side, price, quantity in legs:
        opposite = "SELL" if side == "BUY" else "BUY"
        total += leg_charges(side=side, price=price, quantity=quantity, schedule=schedule).total
        total += leg_charges(side=opposite, price=price, quantity=quantity, schedule=schedule).total
    return total
