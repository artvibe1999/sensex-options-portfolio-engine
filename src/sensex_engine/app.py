"""Process entrypoint: strategy engine + optional Telegram control plane.

* Without ``TELEGRAM_BOT_TOKEN``: headless PAPER engine; alerts go to the log.
* With Telegram configured: allowlisted commands, alerts to allowlisted chats,
  supervised read-only Kite monitor, startup recovery and the fail-closed live
  path (``/resume`` -> ``/live`` -> ``/confirm_live``). Every start is PAPER.

Run: ``python -m sensex_engine``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time as _time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from .book import EngineBook
from .config import Settings
from .engine import Engine, EngineConfig, LiveBridge, Notifier
from .kite import KiteReadOnlyAdapter
from .kite_session import AccessTokenProvider, SharedKiteClient
from .market_data import validate_quote
from .market_feed import MarketFeed
from .reconciliation import configure_scope
from .strategies import PRIORITY, StrategyConfig

logger = logging.getLogger("sensex_engine")


class CachedKiteAdapter(KiteReadOnlyAdapter):
    """Read-only adapter that reuses the BFO master for a few minutes.

    Several services (readiness, recovery, sizing, monitor) ask for the master;
    downloading it each time would waste the shared Kite app's rate budget.
    """

    def __init__(self, client: Any, ttl_seconds: int = 600) -> None:
        super().__init__(client)
        self._ttl = ttl_seconds
        self._loaded_at = 0.0
        self._cache: list = []
        self._master_lock = threading.Lock()

    def instruments(self):  # type: ignore[override]
        with self._master_lock:
            if self._cache and _time.monotonic() - self._loaded_at < self._ttl:
                return list(self._cache)
            contracts = super().instruments()
            self._cache, self._loaded_at = contracts, _time.monotonic()
            return list(contracts)


class AlertGate:
    """De-duplicate monitor alerts: identical problems at most once per ``repeat_after``.

    A recovery is always delivered (once), and the next problem after a recovery is
    delivered immediately.
    """

    def __init__(self, repeat_after_seconds: float = 3600, clock=_time.monotonic) -> None:
        self._repeat = repeat_after_seconds
        self._clock = clock
        self._last: tuple[bool, str] | None = None
        self._last_at = 0.0

    def should_send(self, healthy: bool, reason: str) -> bool:
        now = self._clock()
        key = (healthy, reason)
        if self._last is not None and self._last[0] == healthy:
            if healthy:
                return False  # already reported healthy
            if key == self._last and now - self._last_at < self._repeat:
                return False
        self._last, self._last_at = key, now
        return True


def close_limit_price_provider(adapter: KiteReadOnlyAdapter, *, max_age_seconds: int = 5):
    """Marketable LIMIT price for risk-reducing/entry legs from a fresh quote.

    Unlike the emergency provider it tolerates wider spreads (cheap wings), so a
    protective close is never blocked purely by a wide but valid quote.
    """

    def price_for(action: Any) -> Decimal:
        contracts = {c.instrument_token: c for c in adapter.instruments()}
        contract = contracts.get(action.instrument_token)
        if contract is None or contract.tradingsymbol != action.tradingsymbol:
            raise ValueError("Contract is not in the current BFO master")
        quote = adapter.quotes([contract.instrument_token]).get(contract.instrument_token)
        if quote is None:
            raise ValueError("Quote unavailable")
        quality = validate_quote(
            quote,
            now=datetime.now(UTC),
            max_age_seconds=max_age_seconds,
            max_spread_pct=Decimal("0.25"),
            min_top_depth=1,
            min_volume=0,
        )
        if not quality.valid:
            raise ValueError(f"Quote rejected: {quality.reason}")
        raw = quote.ask if action.side == "BUY" else quote.bid
        units = raw / contract.tick_size
        rounding = ROUND_CEILING if action.side == "BUY" else ROUND_FLOOR
        return units.to_integral_value(rounding=rounding) * contract.tick_size

    return price_for


def telegram_sender(token: str, chat_ids: list[int]):
    def send(text: str) -> None:
        for chat in chat_ids:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()

    return send


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str((getter() if callable(getter) else value) or "").strip()


def _engine_config(settings: Settings) -> EngineConfig:
    enabled = {s.strip() for s in settings.enabled_strategies.split(",") if s.strip()}
    unknown = enabled - set(PRIORITY)
    if unknown:
        raise ValueError(f"Unknown strategies in ENABLED_STRATEGIES: {sorted(unknown)}")
    hh, mm = (int(x) for x in settings.square_off_time.split(":"))
    if not time(14, 30) <= time(hh, mm) <= time(15, 20):
        raise ValueError("SQUARE_OFF_TIME must be between 14:30 and 15:20 IST")
    return EngineConfig(
        cycle_seconds=settings.engine_cycle_seconds,
        square_off=time(hh, mm),
        max_entries_per_day=settings.max_new_entries_per_day,
        max_margin_utilization=Decimal(str(settings.max_margin_utilization_pct)),
        paper_start_capital=(
            Decimal(str(settings.paper_start_capital)) if settings.paper_start_capital > 0 else None
        ),
        risk_scale=Decimal(str(settings.risk_scale)),
        enabled_strategies=frozenset(enabled) or None,
        freeze_quantity=settings.bfo_sensex_freeze_quantity,
        strategy=StrategyConfig(min_score=settings.min_strategy_score),
        daily_stop_override=_explicit(settings, "daily_loss_stop_pct"),
        weekly_stop_override=_explicit(settings, "weekly_drawdown_stop_pct"),
        monthly_stop_override=_explicit(settings, "monthly_drawdown_stop_pct"),
    )


def _explicit(settings: Settings, name: str) -> Decimal | None:
    """Only operator-set values tighten tier stops; library defaults do not."""
    if name not in settings.model_fields_set:
        return None
    return Decimal(str(getattr(settings, name)))


def build(
    settings: Settings, *, post_init: Any = None, post_shutdown: Any = None
) -> tuple[Engine, Any | None, Any]:
    api_key = _secret(settings.kite_api_key)
    if not api_key:
        raise SystemExit("KITE_API_KEY is required (same Kite app as the primary bot)")
    configure_scope(managed=settings.reconcile_scope == "managed")
    provider = AccessTokenProvider(
        token_file=settings.kite_token_file, fallback_token=_secret(settings.kite_access_token)
    )
    shared = SharedKiteClient(api_key, provider)
    info = shared.refresh()
    if not info.token:
        logger.error(
            "No Kite access token yet (file %s); engine will wait for it",
            settings.kite_token_file or "<unset>",
        )
    adapter = CachedKiteAdapter(shared.client)
    feed = MarketFeed(adapter)
    book = EngineBook(settings.engine_db_path)
    notifier = Notifier()
    engine = Engine(
        feed=feed,
        book=book,
        notifier=notifier,
        config=_engine_config(settings),
        token_refresh=shared.refresh,
    )
    application = None
    if settings.telegram_bot_token.strip():
        from .telegram_transport import build_application_from_settings

        runtime_settings = settings.model_copy(
            update={"kite_access_token": info.token or "pending"}
        )

        def spot() -> Decimal | None:
            state = engine.last_state
            return Decimal(str(state.spot)) if state else None

        application = build_application_from_settings(
            runtime_settings,
            broker_factory=lambda _key, _token: adapter,
            portfolio_snapshot_provider=engine.live_snapshot,
            limits_provider=engine.live_limits,
            limit_price_provider=close_limit_price_provider(adapter),
            spot_provider=spot,
            post_init=post_init,
            post_shutdown=post_shutdown,
        )
        operations = application.bot_data["operations_controller"]
        engine.operations = operations
        operations.engine = engine
        readiness = application.bot_data.get("live_readiness_service")
        operations.readiness_reason = lambda: (
            "; ".join(getattr(readiness, "last_reasons", ()) or ()) or "see logs"
        )
        coordinator = application.bot_data.get("live_execution_coordinator")
        if coordinator is not None:
            engine.live = LiveBridge(
                coordinator=coordinator,
                positions=application.bot_data["position_lifecycle"],
                sizing=application.bot_data["entry_sizing"],
                adapter=adapter,
                snapshot_provider=engine.live_snapshot,
            )
        chats = sorted(operations.allowlist.chat_ids)
        notifier.attach(telegram_sender(settings.telegram_bot_token.strip(), chats))
    return engine, application, adapter


def _heartbeat(path: str, engine: Engine, stop: threading.Event) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    while not stop.wait(20):
        try:
            Path(path).write_text(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "last_cycle": engine.last_cycle_at.isoformat()
                        if engine.last_cycle_at
                        else None,
                        "mode": engine.mode,
                        "error": engine.last_error,
                    }
                )
            )
        except OSError:
            logger.exception("Heartbeat write failed")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO; Telegram URLs embed the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings()
    stop = threading.Event()
    holder: dict[str, Any] = {}

    async def post_init(app: Any) -> None:
        from .telegram_transport import build_supervised_monitor_from_settings

        try:  # populate Telegram's "/" command menu
            from telegram import BotCommand

            from .telegram_control import COMMAND_HELP

            await app.bot.set_my_commands(
                [BotCommand(name, menu) for name, menu, _ in COMMAND_HELP]
            )
        except Exception:
            logger.exception("Could not register the Telegram command menu")

        engine_ = holder["engine"]

        gate = AlertGate()

        def alert(healthy: bool, reason: str) -> None:
            if holder.get("shutting_down"):
                return  # the shutdown notice below already explains the state
            if not gate.should_send(healthy, reason):
                return
            if healthy:
                engine_.notifier.send(f"Broker monitor OK: {reason}")
            else:
                hint = ""
                if "not authenticated" in reason:
                    hint = (
                        "\nThe daily Kite token has expired; it refreshes automatically "
                        "after the first bot's next Kite login."
                    )
                engine_.notifier.send(
                    "Broker monitor problem - live gates closed, mode forced to PAPER.\n"
                    f"Reason: {reason}{hint}\nPaper trading continues. (Repeats of this "
                    "alert are muted for an hour.)"
                )

        monitor = build_supervised_monitor_from_settings(
            holder["runtime_settings"],
            store=app.bot_data["portfolio_store"],
            operations=app.bot_data["operations_controller"],
            alert_sender=alert,
            broker_factory=holder["broker_factory"],
        )
        if monitor is not None:
            holder["monitor"] = monitor
            await monitor.start()
        threading.Thread(target=engine_.run_forever, daemon=True, name="engine").start()

    async def post_shutdown(app: Any) -> None:
        holder["shutting_down"] = True
        holder["engine"].notifier.send(
            "Engine stopping (restart, redeploy or VM shutdown). Open positions are kept "
            "in the ledger and resumed on the next start, which always begins in PAPER."
        )
        holder["engine"].stop()
        stop.set()
        monitor = holder.get("monitor")
        if monitor is not None:
            await monitor.stop()

    engine, application, adapter = build(settings, post_init=post_init, post_shutdown=post_shutdown)
    holder.update(
        engine=engine,
        broker_factory=lambda _key, _token: adapter,
        runtime_settings=settings.model_copy(update={"kite_access_token": "shared"}),
    )
    threading.Thread(
        target=_heartbeat,
        args=(settings.heartbeat_path, engine, stop),
        daemon=True,
        name="heartbeat",
    ).start()
    engine.notifier.send(
        "SENSEX portfolio engine started in PAPER mode"
        + (
            " (Telegram control enabled)."
            if application
            else " (headless; Telegram not configured)."
        )
    )

    if application is None:

        def shutdown(*_: Any) -> None:
            engine.stop()
            stop.set()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        engine.run_forever()
        return

    from .telegram_transport import run_startup_recovery_from_settings

    run_startup_recovery_from_settings(
        holder["runtime_settings"],
        store=application.bot_data["portfolio_store"],
        operations=application.bot_data["operations_controller"],
        broker_factory=holder["broker_factory"],
    )
    application.run_polling()


if __name__ == "__main__":
    main()
