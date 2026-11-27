"""Supervised, read-only Kite fill polling and reconciliation.

The monitor repeatedly runs the same ordered recovery transaction used at startup:
replay durable receipts, backfill current-session individual trades, then reconcile
broker positions and orders. Any unhealthy or stale state pauses entries and forces
paper mode. This module never submits, modifies, or cancels broker orders.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

RecoveryFactory = Callable[[], Any]
HealthCallback = Callable[[bool, str], Awaitable[None] | None]


@dataclass(frozen=True)
class BrokerMonitorHealth:
    healthy: bool
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    worker_running: bool
    stale_after_seconds: int


class KiteBrokerMonitor:
    """Run supervised periodic fill backfill and broker reconciliation.

    The recovery service is synchronous and is run in a worker thread so Telegram's
    asyncio event loop remains responsive. A separate watchdog detects a hung broker
    call; it immediately fails closed even if the blocking thread has not returned.
    Reconnection is performed by replacing the recovery service through the supplied
    factory after each failed cycle. Retries are bounded and exponentially backed off.
    """

    def __init__(
        self,
        *,
        recovery_factory: RecoveryFactory,
        on_health_change: HealthCallback,
        poll_interval_seconds: int = 30,
        stale_after_seconds: int = 120,
        retry_max_seconds: int = 30,
    ) -> None:
        if poll_interval_seconds < 10:
            raise ValueError("poll_interval_seconds must be at least 10")
        if stale_after_seconds < poll_interval_seconds * 2:
            raise ValueError("stale_after_seconds must be at least twice the poll interval")
        if retry_max_seconds < 5:
            raise ValueError("retry_max_seconds must be at least 5")
        self._recovery_factory = recovery_factory
        self._on_health_change = on_health_change
        self._poll_interval = poll_interval_seconds
        self._stale_after = stale_after_seconds
        self._retry_max = retry_max_seconds
        self._recovery: Any | None = None
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_success_monotonic: float | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._healthy = False
        self._started_monotonic: float | None = None
        self._state_lock = asyncio.Lock()

    @property
    def health(self) -> BrokerMonitorHealth:
        running = (
            self._poll_task is not None
            and not self._poll_task.done()
            and self._watchdog_task is not None
            and not self._watchdog_task.done()
        )
        age_origin = self._last_success_monotonic or self._started_monotonic
        stale = age_origin is None or time.monotonic() - age_origin > self._stale_after
        return BrokerMonitorHealth(
            healthy=self._healthy and running and not stale,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            worker_running=running,
            stale_after_seconds=self._stale_after,
        )

    async def start(self) -> None:
        """Start the poller and independent stale-state watchdog exactly once."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stop = asyncio.Event()
        self._started_monotonic = time.monotonic()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="kite-fill-monitor")
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="kite-fill-monitor-watchdog"
        )
        self._poll_task.add_done_callback(self._worker_done)
        self._watchdog_task.add_done_callback(self._worker_done)

    async def stop(self) -> None:
        """Stop both tasks and leave the runtime fail-closed."""
        self._stop.set()
        tasks = [task for task in (self._poll_task, self._watchdog_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._set_unhealthy("broker monitoring stopped")

    async def _poll_loop(self) -> None:
        backoff = 1
        try:
            while not self._stop.is_set():
                try:
                    if self._recovery is None:
                        self._recovery = await asyncio.to_thread(self._recovery_factory)
                    result = await asyncio.to_thread(
                        self._recovery.recover,
                        session_date=datetime.now(IST).date(),
                    )
                    if not getattr(result, "recovery_complete", False):
                        reasons = getattr(result, "reasons", ()) or ("recovery did not complete",)
                        raise RuntimeError("; ".join(str(reason) for reason in reasons))
                    self._last_success_monotonic = time.monotonic()
                    self._last_success_at = datetime.now(IST)
                    self._last_error = None
                    self._consecutive_failures = 0
                    backoff = 1
                    await self._set_healthy()
                    await self._wait_or_stop(self._poll_interval)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._consecutive_failures += 1
                    self._last_error = type(exc).__name__
                    await self._set_unhealthy(
                        f"broker polling/reconciliation failed ({type(exc).__name__})"
                    )
                    # Discard the failed client and construct a fresh authenticated
                    # adapter before retrying; never replay a broker order submission.
                    self._recovery = None
                    await self._wait_or_stop(backoff)
                    backoff = min(self._retry_max, max(2, backoff * 2))
        finally:
            if not self._stop.is_set():
                await self._set_unhealthy("broker monitor worker exited unexpectedly")

    async def _watchdog_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self._wait_or_stop(min(5, max(1, self._poll_interval // 3)))
                if self._stop.is_set():
                    break
                if self._poll_task is None or self._poll_task.done():
                    await self._set_unhealthy("broker fill poller worker is not running")
                    break
                origin = self._last_success_monotonic or self._started_monotonic
                if origin is None or time.monotonic() - origin > self._stale_after:
                    await self._set_unhealthy("broker monitoring data is stale")
        finally:
            if not self._stop.is_set():
                await self._set_unhealthy("broker monitor watchdog exited unexpectedly")

    async def _set_healthy(self) -> None:
        async with self._state_lock:
            changed = not self._healthy
            self._healthy = True
            if changed:
                await self._notify(True, "broker fills and reconciliation are current")

    async def _set_unhealthy(self, reason: str) -> None:
        async with self._state_lock:
            changed = self._healthy or self._last_error != reason
            self._healthy = False
            self._last_error = reason
            if changed:
                await self._notify(False, reason)

    async def _notify(self, healthy: bool, reason: str) -> None:
        try:
            result = self._on_health_change(healthy, reason)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Failure to deliver an alert must not stop the watchdog or poller.
            logger.exception("Kite monitor health callback failed")

    async def _wait_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if self._stop.is_set() or task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Kite monitor task terminated: %s", type(error).__name__)
        # The watchdog or poll loop also marks its own unexpected exit unhealthy.
