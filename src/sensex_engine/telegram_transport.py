"""Optional python-telegram-bot transport with audited, fail-closed dispatch."""
from collections.abc import Callable
from typing import Any

from .mode import ModeController
from .persistence import SqliteStore
from .telegram_control import OperationsController

ReadinessProvider = Callable[[], tuple[bool, bool]]

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

    application = Application.builder().token(token).build()

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
) -> Any:
    """Build the actual polling application from environment-backed settings.

    This default entrypoint deliberately supplies no readiness provider, so
    /resume and /confirm_live remain disabled until real safety services exist.
    """
    store = SqliteStore(_sqlite_database_path(settings.database_url))
    from .order_lifecycle import OrderLifecycle
    from .position_lifecycle import PositionLifecycle
    from .portfolio_queries import PortfolioQueryService

    portfolio_queries = PortfolioQueryService(
        positions=PositionLifecycle(store),
        orders=OrderLifecycle(store),
    )
    operations = operations_from_settings(
        settings, portfolio_queries=portfolio_queries
    )
    if not operations.allowlist.user_ids or not operations.allowlist.chat_ids:
        raise ValueError("Telegram user and chat allowlists must both be configured")
    return create_telegram_application(
        settings.telegram_bot_token,
        operations=operations,
        store=store,
        readiness_provider=readiness_provider,
    )


def run_from_environment() -> None:
    """Run the Telegram polling process in paper-only fail-closed configuration."""
    from .config import Settings

    application = build_application_from_settings(Settings())
    application.run_polling()


if __name__ == "__main__":
    run_from_environment()
