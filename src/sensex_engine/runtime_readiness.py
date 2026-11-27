"""Runtime-backed Kite readiness dependencies; missing portfolio history blocks LIVE."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from .capital import adaptive_risk_budget
from .domain import PortfolioSnapshot, Right
from .market_data import validate_quote
from .readiness import LiveReadinessService

IST = ZoneInfo("Asia/Kolkata")


class KitePortfolioRiskReadiness:
    """Validate live broker funds and authoritative portfolio risk before activation.

    Kite funds are checked on every call. A separate authoritative portfolio
    snapshot is mandatory for drawdown, daily loss, open-risk and entry-limit checks.
    Until PE-06 supplies that snapshot, this gate deliberately remains closed.
    """

    def __init__(
        self,
        *,
        broker: Any,
        snapshot_provider: Callable[[], PortfolioSnapshot | None] | None,
        settings: Any,
    ) -> None:
        self._broker = broker
        self._snapshot_provider = snapshot_provider
        self._settings = settings
        self.last_reason = "Portfolio risk snapshot is unavailable"

    @staticmethod
    def _amount(value: Any, field: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"Kite margin field {field} is invalid")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"Kite margin field {field} is invalid") from exc
        if not amount.is_finite():
            raise ValueError(f"Kite margin field {field} is not finite")
        return amount

    def __call__(self) -> bool:
        try:
            margin = self._broker.margins("equity")
            if not isinstance(margin, dict) or margin.get("enabled") is not True:
                raise ValueError("Kite equity/F&O segment is not enabled")
            available = margin.get("available")
            utilised = margin.get("utilised")
            if not isinstance(available, dict) or not isinstance(utilised, dict):
                raise ValueError("Kite margin availability/utilisation is missing")
            net = self._amount(margin.get("net"), "net")
            cash = self._amount(available.get("cash"), "available.cash")
            live_balance = self._amount(
                available.get("live_balance"), "available.live_balance"
            )
            debits = self._amount(utilised.get("debits"), "utilised.debits")
            if net <= 0 or cash <= 0 or live_balance <= 0 or debits < 0:
                raise ValueError("Kite available funds are not positive and usable")
            denominator = net + debits
            utilization = debits / denominator if denominator > 0 else Decimal("1")
            max_utilization = Decimal(str(
                getattr(self._settings, "max_margin_utilization_pct", 0.70)
            ))
            if not max_utilization.is_finite() or not Decimal("0") < max_utilization <= Decimal("1"):
                raise ValueError("Configured margin utilization ceiling is invalid")
            if utilization > max_utilization:
                raise ValueError("Broker margin utilization exceeds the configured ceiling")

            if self._snapshot_provider is None:
                raise ValueError(
                    "Authoritative portfolio equity/drawdown snapshot is unavailable; "
                    "PE-06 accounting must be completed before LIVE"
                )
            portfolio = self._snapshot_provider()
            if not isinstance(portfolio, PortfolioSnapshot):
                raise ValueError("Authoritative portfolio risk snapshot is unavailable")
            if portfolio.peak_equity is None:
                raise ValueError("Portfolio peak equity history is unavailable")
            equity = portfolio.net_liquidation_equity
            if equity <= 0 or portfolio.available_margin <= 0:
                raise ValueError("Portfolio equity or available margin is not positive")
            if portfolio.available_margin > net:
                raise ValueError("Portfolio available margin exceeds broker-reported funds")
            if portfolio.open_positions > int(getattr(
                self._settings, "max_concurrent_positions", 2
            )):
                raise ValueError("Portfolio exceeds maximum concurrent positions")
            if portfolio.entries_today >= int(getattr(
                self._settings, "max_new_entries_per_day", 3
            )):
                raise ValueError("Maximum daily entry count has been reached")

            daily_stop = Decimal(str(getattr(
                self._settings, "daily_loss_stop_pct", 0.02
            )))
            weekly_stop = Decimal(str(getattr(
                self._settings, "weekly_drawdown_stop_pct", 0.04
            )))
            monthly_stop = Decimal(str(getattr(
                self._settings, "monthly_drawdown_stop_pct", 0.07
            )))
            if portfolio.daily_realized_pnl + portfolio.daily_unrealized_pnl <= -equity * daily_stop:
                raise ValueError("Portfolio daily loss stop has been reached")
            if portfolio.weekly_drawdown_pct >= weekly_stop:
                raise ValueError("Portfolio weekly drawdown stop has been reached")
            if portfolio.monthly_drawdown_pct >= monthly_stop:
                raise ValueError("Portfolio monthly drawdown stop has been reached")

            budget = adaptive_risk_budget(
                equity,
                peak_equity=portfolio.peak_equity,
                base_risk_pct=Decimal(str(getattr(
                    self._settings, "risk_per_trade_pct", 0.0075
                ))),
                base_aggregate_pct=Decimal(str(getattr(
                    self._settings, "max_aggregate_open_risk_pct", 0.02
                ))),
            )
            if not budget.new_entries_allowed:
                raise ValueError(budget.reason)
            if portfolio.open_max_loss < 0 or portfolio.open_max_loss > budget.max_aggregate_risk:
                raise ValueError("Existing open risk exceeds the adaptive aggregate risk budget")
            self.last_reason = ""
            return True
        except Exception as exc:
            self.last_reason = str(exc)[:300]
            return False


def build_kite_live_readiness_service(
    *,
    broker: Any,
    store: Any,
    position_lifecycle: Any,
    order_lifecycle: Any,
    settings: Any,
    portfolio_snapshot_provider: Callable[[], PortfolioSnapshot | None] | None = None,
) -> LiveReadinessService:
    """Compose actual Kite contract, quote, persistence, reconciliation and risk gates."""

    contracts_cache: dict[str, Any] = {"contracts": [], "loaded_at": None}

    def contracts_provider():
        contracts = broker.instruments()
        if not isinstance(contracts, list) or not contracts:
            raise ValueError("Kite returned no current BFO SENSEX option contracts")
        tokens = [item.instrument_token for item in contracts]
        symbols = [item.tradingsymbol for item in contracts]
        if (
            any(token <= 0 for token in tokens)
            or len(tokens) != len(set(tokens))
            or len(symbols) != len(set(symbols))
            or any(item.lot_size <= 0 or item.tick_size <= 0 for item in contracts)
        ):
            raise ValueError("BFO SENSEX contract master contains invalid or duplicate metadata")
        timestamp = datetime.now(UTC)
        contracts_cache["contracts"] = contracts
        contracts_cache["loaded_at"] = timestamp
        return contracts, timestamp

    def market_data_timestamp_provider() -> datetime:
        contracts = contracts_cache["contracts"]
        if not contracts:
            raise RuntimeError("Contract master must be refreshed before market-data checks")
        now = datetime.now(UTC)
        eligible = [
            item for item in contracts
            if item.expiry.date() >= now.astimezone(IST).date()
        ]
        if not eligible:
            raise RuntimeError("No unexpired SENSEX option contracts are listed")
        expiry = min(item.expiry for item in eligible)
        expiry_contracts = [item for item in eligible if item.expiry == expiry]
        strikes = sorted({item.strike for item in expiry_contracts})
        if not strikes:
            raise RuntimeError("Nearest SENSEX expiry has no valid strikes")
        central_strike = strikes[len(strikes) // 2]
        pair = [
            item for item in expiry_contracts
            if item.strike == central_strike and item.right in {Right.CE, Right.PE}
        ]
        by_right = {item.right: item for item in pair}
        if Right.CE not in by_right or Right.PE not in by_right:
            raise RuntimeError("Fresh paired CE/PE contracts are unavailable for readiness")
        quotes = broker.quotes([
            by_right[Right.CE].instrument_token,
            by_right[Right.PE].instrument_token,
        ])
        if not isinstance(quotes, dict):
            raise ValueError("Kite quote response is malformed")
        checked = []
        for contract in (by_right[Right.CE], by_right[Right.PE]):
            quote = quotes.get(contract.instrument_token)
            if quote is None:
                raise ValueError("Kite did not return both paired SENSEX option quotes")
            quality = validate_quote(
                quote,
                now=datetime.now(UTC),
                max_age_seconds=2,
                max_spread_pct=Decimal("0.03"),
                min_top_depth=1,
                min_volume=1,
            )
            if not quality.valid:
                raise ValueError(f"SENSEX {contract.right.value} quote rejected: {quality.reason}")
            checked.append(quote.timestamp.astimezone(UTC))
        if abs((checked[0] - checked[1]).total_seconds()) > 2:
            raise ValueError("Paired SENSEX option quotes are not timestamp-aligned")
        return min(checked)

    risk_gate = KitePortfolioRiskReadiness(
        broker=broker,
        snapshot_provider=portfolio_snapshot_provider,
        settings=settings,
    )
    static_ip_confirmed = getattr(settings, "kite_static_ip_confirmed", False) is True

    return LiveReadinessService(
        broker=broker,
        position_lifecycle=position_lifecycle,
        order_lifecycle=order_lifecycle,
        contracts_provider=contracts_provider,
        market_data_timestamp_provider=market_data_timestamp_provider,
        risk_clearance_provider=risk_gate,
        persistence_health_provider=store.health_check,
        execution_readiness_provider=lambda: static_ip_confirmed,
        max_contract_age_seconds=86_400,
        max_market_data_age_seconds=2,
    )
