"""Conservative SENSEX option-chain analytics from validated Kite quotes.

Greeks and implied volatility are model estimates, not exchange-provided values.
"""
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from math import erf, exp, log, pi, sqrt
from zoneinfo import ZoneInfo

from .domain import Contract, Right
from .market_data import Quote, validate_quote

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class OptionAnalytics:
    contract: Contract
    quote: Quote
    mid_price: Decimal
    implied_volatility: Decimal
    delta: Decimal
    gamma: Decimal
    theta_per_day: Decimal
    vega_per_vol_point: Decimal
    rho_per_one_percent: Decimal


@dataclass(frozen=True)
class RejectedContract:
    instrument_token: int
    reason: str


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying_price: Decimal
    as_of: datetime
    risk_free_rate: Decimal
    analytics: tuple[OptionAnalytics, ...]
    rejected: tuple[RejectedContract, ...]


def _expiry_close(expiry: datetime) -> datetime:
    local_date = expiry.astimezone(IST).date() if expiry.tzinfo else expiry.date()
    return datetime.combine(local_date, time(15, 30), tzinfo=IST)


def _cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2.0 * pi)


def _price(
    spot: float, strike: float, years: float, rate: float, vol: float, right: Right
) -> float:
    root_t = sqrt(years)
    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * root_t)
    d2 = d1 - vol * root_t
    discounted_strike = strike * exp(-rate * years)
    if right is Right.CE:
        return spot * _cdf(d1) - discounted_strike * _cdf(d2)
    return discounted_strike * _cdf(-d2) - spot * _cdf(-d1)


def _implied_volatility(
    premium: float, spot: float, strike: float, years: float, rate: float, right: Right
) -> float | None:
    discounted_strike = strike * exp(-rate * years)
    intrinsic = (
        max(spot - discounted_strike, 0.0)
        if right is Right.CE
        else max(discounted_strike - spot, 0.0)
    )
    upper_bound = spot if right is Right.CE else discounted_strike
    if premium < intrinsic - 1e-7 or premium >= upper_bound or premium <= 0:
        return None
    low, high = 1e-4, 5.0
    if _price(spot, strike, years, rate, high, right) < premium:
        return None
    for _ in range(100):
        mid = (low + high) / 2.0
        model_price = _price(spot, strike, years, rate, mid, right)
        if abs(model_price - premium) < 1e-7:
            return mid
        if model_price < premium:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _greeks(
    spot: float, strike: float, years: float, rate: float, vol: float, right: Right
) -> tuple[float, float, float, float, float]:
    root_t = sqrt(years)
    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * root_t)
    d2 = d1 - vol * root_t
    discount = exp(-rate * years)
    gamma = _pdf(d1) / (spot * vol * root_t)
    vega = spot * _pdf(d1) * root_t / 100.0
    if right is Right.CE:
        delta = _cdf(d1)
        theta = (
            -(spot * _pdf(d1) * vol) / (2.0 * root_t)
            - rate * strike * discount * _cdf(d2)
        ) / 365.0
        rho = strike * years * discount * _cdf(d2) / 100.0
    else:
        delta = _cdf(d1) - 1.0
        theta = (
            -(spot * _pdf(d1) * vol) / (2.0 * root_t)
            + rate * strike * discount * _cdf(-d2)
        ) / 365.0
        rho = -strike * years * discount * _cdf(-d2) / 100.0
    return delta, gamma, theta, vega, rho


def build_option_chain(
    contracts: list[Contract],
    quotes: dict[int, Quote],
    *,
    underlying_price: Decimal,
    underlying_timestamp: datetime,
    as_of: datetime,
    risk_free_rate: Decimal,
    max_quote_age_seconds: int = 5,
    max_spread_pct: Decimal = Decimal("0.03"),
) -> OptionChainSnapshot:
    """Build a filtered chain; invalid/unpriced contracts are excluded with reasons.

    Expiry is modelled at 15:30 IST on the exchange expiry date. Theta is per
    calendar day; vega and rho are per one percentage-point change.
    """
    if as_of.tzinfo is None or underlying_timestamp.tzinfo is None:
        raise ValueError("as_of and underlying_timestamp must be timezone-aware")
    if underlying_price <= ZERO:
        raise ValueError("Underlying price must be positive")
    if not Decimal("-0.05") <= risk_free_rate <= Decimal("0.25"):
        raise ValueError("Risk-free rate is outside supported bounds")
    spot_age = (as_of - underlying_timestamp).total_seconds()
    if spot_age < -1 or spot_age > max_quote_age_seconds:
        raise ValueError("Underlying index price is stale or timestamp is in the future")

    now_ist = as_of.astimezone(IST)
    spot = float(underlying_price)
    rate = float(risk_free_rate)
    results: list[OptionAnalytics] = []
    rejected: list[RejectedContract] = []
    seen: set[int] = set()

    for contract in contracts:
        if contract.instrument_token in seen:
            rejected.append(
                RejectedContract(contract.instrument_token, "Duplicate instrument token")
            )
            continue
        seen.add(contract.instrument_token)
        quote = quotes.get(contract.instrument_token)
        if quote is None:
            rejected.append(RejectedContract(contract.instrument_token, "Quote missing"))
            continue
        quality = validate_quote(
            quote, now=as_of, max_age_seconds=max_quote_age_seconds,
            max_spread_pct=max_spread_pct,
        )
        if not quality.valid:
            rejected.append(RejectedContract(contract.instrument_token, quality.reason))
            continue
        if contract.strike <= ZERO or contract.right not in (Right.CE, Right.PE):
            rejected.append(RejectedContract(contract.instrument_token, "Invalid option contract"))
            continue
        if (
            quote.bid % contract.tick_size != ZERO
            or quote.ask % contract.tick_size != ZERO
        ):
            rejected.append(
                RejectedContract(
                    contract.instrument_token, "Quote prices violate contract tick size"
                )
            )
            continue
        expiry = _expiry_close(contract.expiry)
        seconds = (expiry - now_ist).total_seconds()
        if seconds <= 0:
            rejected.append(RejectedContract(contract.instrument_token, "Contract has expired"))
            continue
        years = seconds / (365.0 * 24.0 * 60.0 * 60.0)
        mid = (quote.bid + quote.ask) / Decimal("2")
        iv = _implied_volatility(
            float(mid), spot, float(contract.strike), years, rate, contract.right
        )
        if iv is None or not 0.0001 <= iv <= 5.0:
            rejected.append(
                RejectedContract(
                    contract.instrument_token, "Mid price has no valid implied volatility"
                )
            )
            continue
        delta, gamma, theta, vega, rho = _greeks(
            spot, float(contract.strike), years, rate, iv, contract.right
        )
        results.append(OptionAnalytics(
            contract=contract,
            quote=quote,
            mid_price=mid,
            implied_volatility=Decimal(str(iv)),
            delta=Decimal(str(delta)),
            gamma=Decimal(str(gamma)),
            theta_per_day=Decimal(str(theta)),
            vega_per_vol_point=Decimal(str(vega)),
            rho_per_one_percent=Decimal(str(rho)),
        ))

    results.sort(
        key=lambda item: (
            item.contract.expiry, item.contract.strike, item.contract.right.value
        )
    )
    return OptionChainSnapshot(
        underlying_price=underlying_price,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
        analytics=tuple(results),
        rejected=tuple(rejected),
    )
