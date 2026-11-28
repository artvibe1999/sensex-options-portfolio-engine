"""Full-revaluation scenario engine for single- and multi-expiry structures.

Black-Scholes repricing is a model, not an exchange price. It is used for
stress tests, calendar/diagonal risk and probability estimates. Executable
entry prices always come from bid/ask quotes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from math import erf, exp, log, sqrt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SPOT_SHOCKS = (-0.02, -0.015, -0.01, -0.005, 0.0, 0.005, 0.01, 0.015, 0.02)
IV_SHOCKS = (-0.10, -0.05, -0.03, 0.0, 0.03, 0.05, 0.10)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(
    spot: float, strike: float, years: float, rate: float, vol: float, right: str
) -> float:
    intrinsic = max(spot - strike, 0.0) if right == "CE" else max(strike - spot, 0.0)
    if years <= 0 or vol <= 0:
        return intrinsic
    rt = sqrt(years)
    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * rt)
    d2 = d1 - vol * rt
    disc = strike * exp(-rate * years)
    if right == "CE":
        return spot * _cdf(d1) - disc * _cdf(d2)
    return disc * _cdf(-d2) - spot * _cdf(-d1)


def expiry_close(expiry: datetime) -> datetime:
    day = expiry.astimezone(IST).date() if expiry.tzinfo else expiry.date()
    return datetime.combine(day, time(15, 30), tzinfo=IST)


def years_between(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds(), 0.0) / (365.0 * 86400.0)


@dataclass(frozen=True)
class ScenarioLeg:
    right: str
    strike: float
    expiry: datetime
    side: str  # BUY | SELL
    quantity: int  # units (lots x lot size)
    entry_price: float  # executable entry premium per unit
    iv: float  # model IV for repricing

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1


def structure_value(
    legs: list[ScenarioLeg],
    *,
    spot: float,
    at: datetime,
    rate: float,
    iv_shift: float = 0.0,
) -> float:
    total = 0.0
    for leg in legs:
        years = years_between(at, expiry_close(leg.expiry))
        vol = max(leg.iv + iv_shift, 0.01)
        total += leg.sign * leg.quantity * bs_price(spot, leg.strike, years, rate, vol, leg.right)
    return total


def entry_cost(legs: list[ScenarioLeg]) -> float:
    """Signed net premium paid (positive = debit)."""
    return sum(leg.sign * leg.quantity * leg.entry_price for leg in legs)


@dataclass(frozen=True)
class StressReport:
    worst_pnl: float
    best_pnl: float
    grid: tuple[tuple[float, float, float], ...]  # (spot_shock, iv_shock, pnl)


def stress(
    legs: list[ScenarioLeg],
    *,
    spot: float,
    at: datetime,
    horizon: datetime,
    rate: float,
    costs: float = 0.0,
) -> StressReport:
    """Reprice at ``horizon`` across the spot x IV grid (spec 9.5)."""
    if horizon < at:
        horizon = at
    paid = entry_cost(legs)
    grid = []
    for ds in SPOT_SHOCKS:
        for dv in IV_SHOCKS:
            value = structure_value(legs, spot=spot * (1 + ds), at=horizon, rate=rate, iv_shift=dv)
            grid.append((ds, dv, value - paid - costs))
    pnls = [g[2] for g in grid]
    return StressReport(min(pnls), max(pnls), tuple(grid))


@dataclass(frozen=True)
class Probability:
    pop: float  # probability of profit at expiry
    expected_value: float  # expected P&L at expiry (model, after costs)


def expiry_probability(
    payoff,
    *,
    spot: float,
    iv: float,
    years: float,
    costs: float = 0.0,
    steps: int = 400,
) -> Probability:
    """Integrate a terminal payoff ``payoff(S)`` under a driftless lognormal.

    ``payoff`` returns P&L before costs at underlying S.
    """
    if iv <= 0 or years <= 0 or spot <= 0:
        raise ValueError("Positive spot, IV and time are required")
    sigma = iv * sqrt(years)
    mu = -0.5 * sigma * sigma
    lo, hi = -6.0, 6.0
    width = (hi - lo) / steps
    pop = ev = mass = 0.0
    for i in range(steps):
        z = lo + (i + 0.5) * width
        weight = exp(-0.5 * z * z) * width
        s = spot * exp(mu + sigma * z)
        pnl = payoff(s) - costs
        mass += weight
        ev += weight * pnl
        if pnl > 0:
            pop += weight
    return Probability(pop / mass, ev / mass)
