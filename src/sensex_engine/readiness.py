"""Fail-closed readiness evaluation for any future live-mode activation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .reconciliation import BrokerReconciliationService, ReconciliationReport


@dataclass(frozen=True)
class ReadinessResult:
    broker_ready: bool
    risk_clear: bool
    reasons: tuple[str, ...]
    reconciliation: ReconciliationReport | None = None

    @property
    def ready(self) -> bool:
        return self.broker_ready and self.risk_clear and not self.reasons

    def telegram_flags(self) -> tuple[bool, bool]:
        """Return the existing Telegram transport readiness tuple."""
        return self.broker_ready and not self.reasons, self.risk_clear


class LiveReadinessService:
    """Evaluate broker, contracts, data, positions and risk before live activation.

    All freshness timestamps must be timezone-aware. Risk clearance is supplied by
    the caller so this service never invents portfolio values or risk state.
    """

    def __init__(
        self,
        *,
        broker: Any,
        position_lifecycle: Any,
        contracts_provider: Callable[[], tuple[list[Any], datetime]],
        order_lifecycle: Any | None = None,
        market_data_timestamp_provider: Callable[[], datetime],
        risk_clearance_provider: Callable[[], bool],
        persistence_health_provider: Callable[[], bool] | None = None,
        execution_readiness_provider: Callable[[], bool] | None = None,
        reconciliation_service: BrokerReconciliationService | None = None,
        max_contract_age_seconds: int = 86_400,
        max_market_data_age_seconds: int = 5,
    ) -> None:
        if max_contract_age_seconds <= 0 or max_market_data_age_seconds <= 0:
            raise ValueError("Readiness freshness limits must be positive")
        self.broker = broker
        self.position_lifecycle = position_lifecycle
        self.order_lifecycle = order_lifecycle
        self.contracts_provider = contracts_provider
        self.market_data_timestamp_provider = market_data_timestamp_provider
        self.risk_clearance_provider = risk_clearance_provider
        self.persistence_health_provider = persistence_health_provider
        self.execution_readiness_provider = execution_readiness_provider
        self.reconciliation_service = reconciliation_service or BrokerReconciliationService()
        self.max_contract_age_seconds = max_contract_age_seconds
        self.max_market_data_age_seconds = max_market_data_age_seconds

    @staticmethod
    def _fresh(timestamp: datetime, *, now: datetime, max_age: int) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return False
        age = (now - timestamp).total_seconds()
        return -1 <= age <= max_age

    def evaluate(self, *, now: datetime | None = None) -> ReadinessResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Readiness evaluation time must be timezone-aware")
        reasons: list[str] = []
        try:
            if not self.broker.is_authenticated():
                reasons.append("Kite broker authentication is not valid")
        except Exception as exc:
            reasons.append(f"Kite authentication check failed: {type(exc).__name__}")
        if self.persistence_health_provider is None:
            reasons.append("Durable persistence health provider is not configured")
        else:
            try:
                if self.persistence_health_provider() is not True:
                    reasons.append("Durable persistence health check is not affirmative")
            except Exception as exc:
                reasons.append(f"Durable persistence check failed: {type(exc).__name__}")
        if self.execution_readiness_provider is None:
            reasons.append("Live order execution readiness provider is not configured")
        else:
            try:
                if self.execution_readiness_provider() is not True:
                    reasons.append("Live order execution prerequisites are not satisfied")
            except Exception as exc:
                reasons.append(f"Live order execution readiness failed: {type(exc).__name__}")
        contracts: list[Any] = []
        contract_timestamp: datetime | None = None
        try:
            contracts, contract_timestamp = self.contracts_provider()
            if not contracts:
                reasons.append("Current BFO contract master is empty")
            if not isinstance(contract_timestamp, datetime) or not self._fresh(
                contract_timestamp, now=current, max_age=self.max_contract_age_seconds
            ):
                reasons.append("BFO contract master is stale or has invalid timestamp")
        except Exception as exc:
            reasons.append(f"Contract-master check failed: {type(exc).__name__}")
        try:
            market_timestamp = self.market_data_timestamp_provider()
            if not isinstance(market_timestamp, datetime) or not self._fresh(
                market_timestamp, now=current, max_age=self.max_market_data_age_seconds
            ):
                reasons.append("Market data is stale or has invalid timestamp")
        except Exception as exc:
            reasons.append(f"Market-data check failed: {type(exc).__name__}")
        reconciliation: ReconciliationReport | None = None
        try:
            reconciliation = self.reconciliation_service.reconcile_store(
                self.broker, contracts=contracts,
                position_lifecycle=self.position_lifecycle,
                order_lifecycle=self.order_lifecycle,
            )
            if not reconciliation.reconciled:
                reasons.extend(reconciliation.mismatches or ("Broker reconciliation failed",))
        except Exception as exc:
            reasons.append(f"Broker reconciliation check failed: {type(exc).__name__}")
        try:
            risk_clear = self.risk_clearance_provider() is True
            if not risk_clear:
                detail = getattr(self.risk_clearance_provider, "last_reason", None)
                reasons.append(
                    str(detail) if detail else "Portfolio risk clearance is not affirmative"
                )
        except Exception as exc:
            risk_clear = False
            reasons.append(f"Portfolio risk check failed: {type(exc).__name__}")
        broker_ready = not reasons and reconciliation is not None and reconciliation.reconciled
        return ReadinessResult(
            broker_ready=broker_ready, risk_clear=risk_clear,
            reasons=tuple(reasons), reconciliation=reconciliation,
        )

    def __call__(self) -> tuple[bool, bool]:
        """Compatibility with Telegram ReadinessProvider; failures always deny."""
        try:
            return self.evaluate().telegram_flags()
        except Exception:
            return False, False
