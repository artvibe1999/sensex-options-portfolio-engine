"""Optional python-telegram-bot transport with audited, fail-closed dispatch."""
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .broker_monitor import KiteBrokerMonitor
from .mode import ModeController
from .persistence import SqliteStore
from .telegram_control import OperationsController

ReadinessProvider = Callable[[], tuple[bool, bool]]
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

COMMANDS = (
    "help", "status", "paper", "live", "confirm_live",
    "pause", "resume", "positions", "orders", "pnl",
    "capital", "risk", "strategies", "close", "closeall", "emergency",
)


async def dispatch_command(
    update: Any,
    context: Any,
    *,
    operations: OperationsController,
    store: SqliteStore,
    readiness_provider: ReadinessProvider | None = None,
) -> None:
    """Dispatch one Telegram update; persist receipt before changing runtime state."""
    message = getattr(update, "effective_message", None)
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    user_id = getattr(user, "id", None)
    chat_id = getattr(chat, "id", None)
    update_id = getattr(update, "update_id", None)
    raw_text = getattr(message, "text", "") or ""
    command = raw_text.strip().split()[0].split("@", 1)[0].lower() if raw_text.strip() else ""

    async def reply(text: str) -> None:
        if message is not None:
            await message.reply_text(text)

    if not isinstance(user_id, int) or not isinstance(chat_id, int) or update_id is None:
        await reply("Command rejected: Telegram identity metadata is missing.")
        return
    if not command.startswith("/"):
        await reply("Unknown command. Use /help.")
        return

    command_id = f"telegram:{update_id}"
    try:
        inserted = store.record_command(
            command_id=command_id,
            user_id=user_id,
            chat_id=chat_id,
            command=command,
            outcome="RECEIVED",
        )
    except Exception:
        await reply("Command rejected: audit storage is unavailable; no state change was made.")
        return
    if not inserted:
        await reply("Duplicate command ignored.")
        return

    broker_ready, risk_clear = False, False
    if command in {"/resume", "/confirm_live"} and readiness_provider is not None:
        try:
            broker_ready, risk_clear = readiness_provider()
        except Exception:
            broker_ready, risk_clear = False, False

    try:
        response = operations.handle(
            command,
            user_id=user_id,
            chat_id=chat_id,
            broker_ready=broker_ready,
            risk_clear=risk_clear,
        )
        outcome = "ACCEPTED"
    except PermissionError:
        response, outcome = "Command rejected: user or chat is not authorized.", "DENIED"
    except NotImplementedError:
        response, outcome = (
            "This command is not enabled yet; broker reconciliation and "
            "position services are pending.",
            "NOT_IMPLEMENTED",
        )
    except (RuntimeError, ValueError) as exc:
        response, outcome = f"Command rejected: {exc}", "REJECTED"
    except Exception:
        response, outcome = "Command failed unexpectedly; entries remain paused.", "ERROR"

    try:
        updated = store.update_command_outcome(
            command_id=command_id, outcome=outcome, detail=response
        )
    except Exception:
        updated = False
    if not updated:
        # If audit finalization fails, never leave the runtime enabled.
        operations.paused = True
        operations.mode.force_paper()
        response = (
            "Audit finalization failed. Runtime has been forced to paper mode "
            "and entries paused; inspect the audit database before resuming."
        )
    await reply(response)


def create_telegram_application(
    token: str,
    *,
    operations: OperationsController,
    store: SqliteStore,
    readiness_provider: ReadinessProvider | None = None,
    post_init: Callable[[Any], Any] | None = None,
    post_shutdown: Callable[[Any], Any] | None = None,
) -> Any:
    """Create a polling-capable Telegram Application; caller controls process lifecycle."""
    if not token.strip():
        raise ValueError("Telegram bot token is required")
    try:
        from telegram.ext import Application, CommandHandler
    except ImportError as exc:
        raise RuntimeError(
            "Install the optional Telegram dependency: pip install -e '.[telegram]'"
        ) from exc

    builder = Application.builder().token(token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    application = builder.build()

    async def callback(update: Any, context: Any) -> None:
        await dispatch_command(
            update,
            context,
            operations=operations,
            store=store,
            readiness_provider=readiness_provider,
        )

    application.add_handler(CommandHandler(list(COMMANDS), callback))
    return application


def operations_from_settings(
    settings: Any, *, portfolio_queries: Any | None = None,
) -> OperationsController:
    """Build the allowlisted command policy from comma-separated settings IDs."""
    from .telegram_auth import TelegramAllowlist, parse_id_allowlist

    allowlist = TelegramAllowlist(
        user_ids=parse_id_allowlist(settings.telegram_allowed_user_ids),
        chat_ids=parse_id_allowlist(settings.telegram_allowed_chat_ids),
    )
    return OperationsController(
        allowlist=allowlist, mode=ModeController(),
        portfolio_queries=portfolio_queries,
    )


def _sqlite_database_path(database_url: str) -> str:
    if database_url == "sqlite:///:memory:":
        raise ValueError("In-memory SQLite is unsupported by the per-operation audit store")
    if database_url.startswith("sqlite:////"):
        return "/" + database_url[len("sqlite:////"):]
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    raise ValueError("Telegram transport currently requires a SQLite DATABASE_URL")


def build_application_from_settings(
    settings: Any,
    *,
    readiness_provider: ReadinessProvider | None = None,
    post_init: Callable[[Any], Any] | None = None,
    post_shutdown: Callable[[Any], Any] | None = None,
) -> Any:
    """Build the actual polling application from environment-backed settings.

    This default entrypoint deliberately supplies no readiness provider, so
    /resume and /confirm_live remain disabled until real safety services exist.
    """
    store = SqliteStore(_sqlite_database_path(settings.database_url))
    from .order_lifecycle import OrderLifecycle
    from .portfolio_queries import PortfolioQueryService
    from .position_lifecycle import PositionLifecycle

    portfolio_queries = PortfolioQueryService(
        positions=PositionLifecycle(store),
        orders=OrderLifecycle(store),
    )
    operations = operations_from_settings(
        settings, portfolio_queries=portfolio_queries
    )
    if not operations.allowlist.user_ids or not operations.allowlist.chat_ids:
        raise ValueError("Telegram user and chat allowlists must both be configured")
    application = create_telegram_application(
        settings.telegram_bot_token,
        operations=operations,
        store=store,
        readiness_provider=readiness_provider,
        post_init=post_init,
        post_shutdown=post_shutdown,
    )
    application.bot_data["operations_controller"] = operations
    application.bot_data["portfolio_store"] = store
    return application


def run_startup_recovery_from_settings(
    settings: Any,
    *,
    store: SqliteStore,
    operations: OperationsController,
    broker_factory: Callable[[str, str], Any] | None = None,
) -> Any | None:
    """Run read-only Kite recovery before Telegram polling when credentials exist.

    Missing credentials deliberately leave the startup gate closed; they never
    prevent the paper-mode Telegram process from starting.
    """
    operations.startup_recovery_complete = False
    def secret_value(value: Any) -> str:
        getter = getattr(value, "get_secret_value", None)
        raw = getter() if callable(getter) else value
        return str(raw or "").strip()

    api_key = secret_value(getattr(settings, "kite_api_key", ""))
    access_token = secret_value(getattr(settings, "kite_access_token", ""))
    if not api_key or not access_token:
        return None

    from .broker_fill_ingestion import BrokerFillIngestionService
    from .fill_coordinator import FillCoordinator
    from .kite import KiteReadOnlyAdapter
    from .kite_fill_transport import KiteTradeHistoryIngestor
    from .order_lifecycle import OrderLifecycle
    from .position_lifecycle import PositionLifecycle
    from .startup_recovery import KiteStartupRecoveryService

    factory = broker_factory or KiteReadOnlyAdapter.from_credentials
    try:
        broker = factory(api_key, access_token)
        positions = PositionLifecycle(store)
        orders = OrderLifecycle(store)
        ingestion = BrokerFillIngestionService(FillCoordinator(orders, positions))
        history = KiteTradeHistoryIngestor(
            client=broker, orders=orders, positions=positions, ingestion=ingestion,
        )
        recovery = KiteStartupRecoveryService(
            broker=broker,
            contracts_provider=broker.instruments,
            position_lifecycle=positions,
            order_lifecycle=orders,
            fill_ingestion=ingestion,
            trade_history=history,
        )
        result = recovery.recover(session_date=datetime.now(IST).date())
        operations.startup_recovery_complete = result.recovery_complete
        if not result.recovery_complete:
            logger.error("Kite startup recovery blocked: %s", "; ".join(result.reasons))
        else:
            logger.info(
                "Kite startup recovery completed: receipts=%d fills=%d",
                result.recovered_receipts, result.backfilled_fills,
            )
        return result
    except Exception as exc:
        operations.startup_recovery_complete = False
        logger.error("Kite startup recovery could not run: %s", type(exc).__name__)
        return None




def build_supervised_monitor_from_settings(
    settings: Any,
    *,
    store: SqliteStore,
    operations: OperationsController,
    alert_sender: Callable[[bool, str], Any] | None = None,
    broker_factory: Callable[[str, str], Any] | None = None,
) -> KiteBrokerMonitor | None:
    """Build the recurring read-only monitor; missing credentials keep it absent.

    Each reconnect constructs a fresh Kite client and fresh service wrappers while
    reusing the same durable SQLite store. A failed/stale cycle forces paper mode,
    pauses entries, and closes the startup recovery gate. A healthy later cycle
    reopens only the recovery gate; it never unpauses or re-enables live trading.
    """
    def secret_value(value: Any) -> str:
        getter = getattr(value, "get_secret_value", None)
        raw = getter() if callable(getter) else value
        return str(raw or "").strip()

    api_key = secret_value(getattr(settings, "kite_api_key", ""))
    access_token = secret_value(getattr(settings, "kite_access_token", ""))
    if not api_key or not access_token:
        return None

    from .broker_fill_ingestion import BrokerFillIngestionService
    from .fill_coordinator import FillCoordinator
    from .kite import KiteReadOnlyAdapter
    from .kite_fill_transport import KiteTradeHistoryIngestor
    from .order_lifecycle import OrderLifecycle
    from .position_lifecycle import PositionLifecycle
    from .startup_recovery import KiteStartupRecoveryService

    factory = broker_factory or KiteReadOnlyAdapter.from_credentials

    def recovery_factory() -> Any:
        broker = factory(api_key, access_token)
        positions = PositionLifecycle(store)
        orders = OrderLifecycle(store)
        ingestion = BrokerFillIngestionService(FillCoordinator(orders, positions))
        history = KiteTradeHistoryIngestor(
            client=broker, orders=orders, positions=positions, ingestion=ingestion,
        )
        return KiteStartupRecoveryService(
            broker=broker,
            contracts_provider=broker.instruments,
            position_lifecycle=positions,
            order_lifecycle=orders,
            fill_ingestion=ingestion,
            trade_history=history,
        )

    async def on_health_change(healthy: bool, reason: str) -> None:
        if healthy:
            operations.startup_recovery_complete = True
            logger.info("Kite broker monitor recovered: %s", reason)
        else:
            operations.startup_recovery_complete = False
            operations.paused = True
            operations.mode.force_paper()
            logger.error("Kite broker monitor fail-closed: %s", reason)
        if alert_sender is not None:
            try:
                result = alert_sender(healthy, reason)
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.exception("Kite broker monitor alert delivery failed")

    return KiteBrokerMonitor(
        recovery_factory=recovery_factory,
        on_health_change=on_health_change,
        poll_interval_seconds=int(
            getattr(settings, "kite_monitor_poll_interval_seconds", 30)
        ),
        stale_after_seconds=int(
            getattr(settings, "kite_monitor_max_stale_seconds", 120)
        ),
        retry_max_seconds=int(
            getattr(settings, "kite_monitor_retry_max_seconds", 30)
        ),
    )


def run_from_environment() -> None:
    """Run Telegram polling after a read-only, fail-closed Kite recovery attempt."""
    from .config import Settings

    settings = Settings()
    monitor_holder: dict[str, KiteBrokerMonitor] = {}

    async def post_init(application: Any) -> None:
        operations = application.bot_data["operations_controller"]
        store = application.bot_data["portfolio_store"]
        allowed_chats = sorted(operations.allowlist.chat_ids)

        async def send_monitor_alert(healthy: bool, reason: str) -> None:
            status = "RECOVERED" if healthy else "UNHEALTHY — FORCED PAPER MODE; ENTRIES PAUSED"
            message = f"SENSEX broker monitor: {status}. {reason}"
            for chat_id in allowed_chats:
                try:
                    await application.bot.send_message(chat_id=chat_id, text=message)
                except Exception:
                    logger.exception("Could not deliver broker monitor alert to allowlisted chat")

        monitor = build_supervised_monitor_from_settings(
            settings,
            store=store,
            operations=operations,
            alert_sender=send_monitor_alert,
        )
        if monitor is not None:
            monitor_holder["monitor"] = monitor
            application.bot_data["broker_monitor"] = monitor
            await monitor.start()

    async def post_shutdown(application: Any) -> None:
        monitor = monitor_holder.get("monitor")
        if monitor is not None:
            await monitor.stop()

    application = build_application_from_settings(
        settings, post_init=post_init, post_shutdown=post_shutdown,
    )
    run_startup_recovery_from_settings(
        settings,
        store=application.bot_data["portfolio_store"],
        operations=application.bot_data["operations_controller"],
    )
    application.run_polling()


if __name__ == "__main__":
    run_from_environment()
