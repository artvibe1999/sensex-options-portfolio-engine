import asyncio
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
