"""Ordered, fail-closed startup recovery for the Kite-backed ledger.

This coordinator repairs durable fill receipts, backfills the current IST session,
and only then compares the recovered ledger with broker positions and orders.
It never submits, modifies, or cancels broker orders.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .broker_fill_ingestion import BrokerFillIngestionService
from .kite_fill_transport import KiteTradeHistoryIngestor
from .reconciliation import BrokerReconciliationService, ReconciliationReport

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class StartupRecoveryResult:
    recovery_complete: bool
    recovered_receipts: int
    backfilled_fills: int
    reconciliation: ReconciliationReport | None
    reasons: tuple[str, ...]


class KiteStartupRecoveryService:
    """Recover local receipts and today's Kite fills before reconciling exposure.

    The caller must invoke this service during startup. A completed reconciliation
    is only one startup gate: it is not live readiness or permission to resume entries.
    The full LiveReadinessService and Telegram confirmation gates must still pass.
    This is an orchestration primitive, not a scheduler or execution service.
    """

    def __init__(
        self,
        *,
        broker: Any,
        contracts_provider: Callable[[], list[Any]],
        position_lifecycle: Any,
        order_lifecycle: Any,
        fill_ingestion: BrokerFillIngestionService,
        trade_history: KiteTradeHistoryIngestor,
        reconciliation: BrokerReconciliationService | None = None,
    ) -> None:
        self._broker = broker
        self._contracts_provider = contracts_provider
        self._positions = position_lifecycle
        self._orders = order_lifecycle
        self._fill_ingestion = fill_ingestion
        self._trade_history = trade_history
        self._reconciliation = reconciliation or BrokerReconciliationService()

    def recover(self, *, session_date: date) -> StartupRecoveryResult:
        """Run local replay -> current-session backfill -> broker reconciliation."""
        if not isinstance(session_date, date) or isinstance(session_date, datetime):
            raise ValueError("session_date must be an explicit calendar date")
        if session_date != datetime.now(IST).date():
            raise ValueError("Startup recovery is restricted to the current IST session")

        recovered_receipts = 0
        backfilled_fills = 0
        try:
            receipts = self._fill_ingestion.recover_pending()
            recovered_receipts = len(receipts)
            fills = self._trade_history.poll_current_session(session_date=session_date)
            backfilled_fills = len(fills)
            contracts = self._contracts_provider()
            if not isinstance(contracts, list) or not contracts:
                return StartupRecoveryResult(
                    recovery_complete=False,
                    recovered_receipts=recovered_receipts,
                    backfilled_fills=backfilled_fills,
                    reconciliation=None,
                    reasons=("Current BFO contract master is unavailable or empty",),
                )
            report = self._reconciliation.reconcile_store(
                self._broker,
                contracts=contracts,
                position_lifecycle=self._positions,
                order_lifecycle=self._orders,
            )
            return StartupRecoveryResult(
                recovery_complete=report.reconciled,
                recovered_receipts=recovered_receipts,
                backfilled_fills=backfilled_fills,
                reconciliation=report,
                reasons=() if report.reconciled else (
                    report.mismatches or ("Broker reconciliation did not pass",)
                ),
            )
        except Exception as exc:
            return StartupRecoveryResult(
                recovery_complete=False,
                recovered_receipts=recovered_receipts,
                backfilled_fills=backfilled_fills,
                reconciliation=None,
                reasons=(f"Startup recovery failed: {type(exc).__name__}",),
            )
