"""Telegram command policy layer; transport handlers can delegate here."""
from dataclasses import dataclass
from typing import Any

from .mode import ModeController
from .telegram_auth import TelegramAllowlist


@dataclass
class OperationsController:
    allowlist: TelegramAllowlist
    mode: ModeController
    paused: bool = True
    portfolio_queries: Any | None = None
    startup_recovery_complete: bool = False

    def handle(
        self,
        command: str,
        *,
        user_id: int | None,
        chat_id: int | None,
        broker_ready: bool = False,
        risk_clear: bool = False,
    ) -> str:
        if not self.allowlist.allows(user_id, chat_id):
            raise PermissionError("Telegram user/chat is not authorized")
        normalized = command.strip().split()[0].lower() if command.strip() else ""
        if normalized == "/status":
            pending = self.mode.live_requested_by is not None
            return (
                f"mode={self.mode.mode.value}; paused={self.paused}; "
                f"live_confirmation_pending={pending}; "
                f"startup_recovery_complete={self.startup_recovery_complete}"
            )
        if normalized == "/help":
            return (
                "Commands: /status /paper /live /confirm_live /pause /resume. "
                "Read-only portfolio queries and position closure are not enabled yet."
            )
        if normalized == "/paper":
            self.mode.force_paper()
            self.paused = True
            return "Paper mode active; entries remain paused until /resume."
        if normalized == "/live":
            self.mode.request_live(user_id, authorized=True)
            return (
                "Live activation requested. Send /confirm_live separately "
                "after reviewing status."
            )
        if normalized == "/confirm_live":
            if not self.startup_recovery_complete:
                raise RuntimeError("Startup recovery has not completed successfully")
            if self.paused:
                raise RuntimeError("Resume checks must pass before live activation")
            self.mode.confirm_live(
                user_id,
                authorized=True,
                broker_ready=broker_ready,
                risk_clear=risk_clear,
            )
            return "Live mode activated after confirmation and readiness checks."
        if normalized == "/pause":
            self.paused = True
            return "New entries paused."
        if normalized == "/resume":
            if not self.startup_recovery_complete:
                raise RuntimeError("Cannot resume: startup recovery has not completed")
            if not broker_ready or not risk_clear:
                raise RuntimeError("Cannot resume: broker readiness or risk checks failed")
            self.paused = False
            return "Entries resumed after readiness checks."
        if normalized in {"/positions", "/orders"}:
            if self.portfolio_queries is None:
                raise NotImplementedError(
                    "Read-only portfolio ledger services are not configured."
                )
            if normalized == "/positions":
                return self.portfolio_queries.positions_text()
            return self.portfolio_queries.orders_text()
        if normalized in {"/pnl", "/capital", "/risk", "/strategies"}:
            raise NotImplementedError(
                "This query requires authoritative account, risk or strategy-runtime services."
            )
        if normalized in {"/close", "/closeall", "/emergency"}:
            raise NotImplementedError(
                "Position closure is disabled until reconciled execution workflows exist."
            )
        raise ValueError("Unknown command. Use /help.")
