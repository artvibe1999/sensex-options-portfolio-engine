import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sensex_engine.broker_monitor import KiteBrokerMonitor

IST = ZoneInfo("Asia/Kolkata")


class HealthyRecovery:
    def __init__(self):
        self.calls = 0

    def recover(self, *, session_date):
        assert session_date == datetime.now(IST).date()
        self.calls += 1
        return SimpleNamespace(recovery_complete=True, reasons=())


class FailedRecovery:
    def recover(self, *, session_date):
        return SimpleNamespace(
            recovery_complete=False,
            reasons=("broker position mismatch",),
        )


def test_monitor_starts_supervises_and_stops_fail_closed():
    async def scenario():
        recovery = HealthyRecovery()
        changes = []
        monitor = KiteBrokerMonitor(
            recovery_factory=lambda: recovery,
            on_health_change=lambda healthy, reason: changes.append((healthy, reason)),
            poll_interval_seconds=10,
            stale_after_seconds=30,
        )
        await monitor.start()
        for _ in range(50):
            if monitor.health.healthy:
                break
            await asyncio.sleep(0.01)
        assert monitor.health.healthy
        assert recovery.calls >= 1
        await monitor.stop()
        assert not monitor.health.healthy
        assert changes[0][0] is True
        assert changes[-1][0] is False
        assert "stopped" in changes[-1][1]

    asyncio.run(scenario())


def test_monitor_fails_closed_on_reconciliation_mismatch_and_retries():
    async def scenario():
        changes = []
        created = []

        def factory():
            created.append(object())
            return FailedRecovery()

        monitor = KiteBrokerMonitor(
            recovery_factory=factory,
            on_health_change=lambda healthy, reason: changes.append((healthy, reason)),
            poll_interval_seconds=10,
            stale_after_seconds=30,
        )
        await monitor.start()
        for _ in range(100):
            if changes:
                break
            await asyncio.sleep(0.01)
        assert changes
        assert changes[0][0] is False
        assert "broker position mismatch" in changes[0][1]
        assert len(created) == 1
        await monitor.stop()

    asyncio.run(scenario())


def test_monitor_rejects_unsafe_poll_and_stale_intervals():
    with pytest.raises(ValueError, match="at least 10"):
        KiteBrokerMonitor(
            recovery_factory=lambda: HealthyRecovery(),
            on_health_change=lambda *_: None,
            poll_interval_seconds=5,
            stale_after_seconds=30,
        )
    with pytest.raises(ValueError, match="twice the poll interval"):
        KiteBrokerMonitor(
            recovery_factory=lambda: HealthyRecovery(),
            on_health_change=lambda *_: None,
            poll_interval_seconds=15,
            stale_after_seconds=20,
        )
    with pytest.raises(ValueError, match="at least 5"):
        KiteBrokerMonitor(
            recovery_factory=lambda: HealthyRecovery(),
            on_health_change=lambda *_: None,
            poll_interval_seconds=10,
            stale_after_seconds=30,
            retry_max_seconds=2,
        )


def test_monitor_health_callback_failure_does_not_kill_supervisor():
    async def scenario():
        recovery = HealthyRecovery()
        monitor = KiteBrokerMonitor(
            recovery_factory=lambda: recovery,
            on_health_change=lambda *_: (_ for _ in ()).throw(RuntimeError("alert down")),
            poll_interval_seconds=10,
            stale_after_seconds=30,
        )
        await monitor.start()
        for _ in range(50):
            if monitor.health.healthy:
                break
            await asyncio.sleep(0.01)
        assert monitor.health.healthy
        assert recovery.calls >= 1
        await monitor.stop()

    asyncio.run(scenario())



def test_monitor_rebuilds_broker_service_after_failure_and_recovers():
    async def scenario():
        changes = []
        created = []

        def factory():
            if not created:
                service = FailedRecovery()
            else:
                service = HealthyRecovery()
            created.append(service)
            return service

        monitor = KiteBrokerMonitor(
            recovery_factory=factory,
            on_health_change=lambda healthy, reason: changes.append((healthy, reason)),
            poll_interval_seconds=10,
            stale_after_seconds=30,
            retry_max_seconds=5,
        )
        await monitor.start()
        for _ in range(300):
            if any(healthy for healthy, _ in changes):
                break
            await asyncio.sleep(0.01)
        assert len(created) >= 2
        assert changes[0][0] is False
        assert any(healthy for healthy, _ in changes)
        await monitor.stop()

    asyncio.run(scenario())


def test_watchdog_fails_closed_when_broker_call_stalls():
    async def scenario():
        release = threading.Event()
        stale_alert = asyncio.Event()

        class StalledRecovery:
            def recover(self, *, session_date):
                release.wait(timeout=3)
                return SimpleNamespace(
                    recovery_complete=False,
                    reasons=("broker response timed out",),
                )

        monitor = None

        async def on_health(healthy, reason):
            if not healthy and "stale" in reason:
                stale_alert.set()
                release.set()

        monitor = KiteBrokerMonitor(
            recovery_factory=lambda: StalledRecovery(),
            on_health_change=on_health,
            poll_interval_seconds=10,
            stale_after_seconds=20,
        )

        async def immediate_wait(_seconds):
            await asyncio.sleep(0)

        monitor._wait_or_stop = immediate_wait
        await monitor.start()
        monitor._started_monotonic = time.monotonic() - 30
        await asyncio.wait_for(stale_alert.wait(), timeout=1)
        await monitor.stop()
        assert not monitor.health.healthy

    asyncio.run(scenario())


def test_monitor_runs_account_refresh_after_recovery_and_fails_closed():
    async def scenario():
        recovery = HealthyRecovery()
        changes = []
        refresh_calls = []

        def refresh():
            refresh_calls.append(recovery.calls)
            return None

        monitor = KiteBrokerMonitor(
            recovery_factory=lambda: recovery,
            on_health_change=lambda healthy, reason: changes.append((healthy, reason)),
            poll_interval_seconds=10,
            stale_after_seconds=30,
            after_recovery=refresh,
        )
        await monitor.start()
        for _ in range(100):
            if changes:
                break
            await asyncio.sleep(0.01)
        assert refresh_calls == [1]
        assert changes and changes[0][0] is False
        assert "Account refresh unavailable" in changes[0][1]
        assert not monitor.health.healthy
        await monitor.stop()

    asyncio.run(scenario())
