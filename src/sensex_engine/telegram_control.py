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
    emergency_close_all: Any | None = None
    emergency_active: bool = False
    # Strategy engine (optional). When attached, paper entries are controlled by
    # /pause and /resume independently of the live readiness gates.
    engine: Any | None = None
    readiness_reason: Any | None = None

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
        if self.emergency_active and normalized in {"/live", "/confirm_live", "/resume"}:
            raise RuntimeError(
                "Emergency close-all is in progress; entries remain halted in paper mode"
            )
        if normalized == "/status":
            pending = self.mode.live_requested_by is not None
            text = (
                f"mode={self.mode.mode.value}; live_gates_paused={self.paused}; "
                f"live_confirmation_pending={pending}; "
                f"startup_recovery_complete={self.startup_recovery_complete}"
            )
            if self.engine is not None:
                text += "\n" + self.engine.status_text()
            return text
        if normalized == "/help":
            return (
                "Commands: /status /paper /live /confirm_live /pause /resume /positions /orders "
                "/pnl /capital /risk /strategies /close <id> confirm /closeall /emergency. "
                "PAPER is the default on every start. LIVE needs /resume (all readiness gates), "
                "then /live and a separate /confirm_live. Emergency close-all immediately "
                "halts entries and forces paper mode."
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
            if self.engine is not None:
                self.engine.user_paused = True
            return "New entries paused (paper and live). Open positions are still managed."
        if normalized == "/resume" and self.engine is not None:
            self.engine.user_paused = False
            if self.startup_recovery_complete and broker_ready and risk_clear:
                self.paused = False
                return "Paper entries resumed. Live readiness gates passed; /live may be requested."
            reason = ""
            if self.readiness_reason is not None:
                try:
                    reason = f" ({self.readiness_reason()})"
                except Exception:
                    reason = ""
            return ("Paper entries resumed. Live gates remain closed: startup recovery, broker "
                    f"readiness or risk checks did not pass{reason}.")
        if normalized == "/resume":
            if not self.startup_recovery_complete:
                raise RuntimeError("Cannot resume: startup recovery has not completed")
            if not broker_ready or not risk_clear:
                raise RuntimeError("Cannot resume: broker readiness or risk checks failed")
            self.paused = False
            return "Entries resumed after readiness checks."
        if normalized == "/positions" and self.engine is not None:
            return self.engine.positions_text()
        if normalized in {"/pnl", "/capital", "/risk"} and self.engine is not None:
            if normalized == "/pnl":
                return self.engine.pnl_text()
            return self.engine.capital_text()
        if normalized == "/strategies" and self.engine is not None:
            return self.engine.strategies_text()
        if normalized == "/close" and self.engine is not None:
            parts = command.split()
            if len(parts) != 3 or parts[2].lower() != "confirm":
                return "Usage: /close <position_id> confirm  (see /positions for ids)"
            return self.engine.close_one(parts[1], user_id=user_id)
        if normalized in {"/positions", "/orders"}:
            if self.portfolio_queries is None:
                raise NotImplementedError(
                    "Read-only portfolio ledger services are not configured."
                )
            if normalized == "/positions":
                return self.portfolio_queries.positions_text()
            return self.portfolio_queries.orders_text()
        if normalized in {"/pnl", "/capital", "/risk"}:
            if self.portfolio_queries is None:
                raise NotImplementedError("Account reporting services are not configured.")
            if normalized == "/pnl":
                return self.portfolio_queries.pnl_text()
            if normalized == "/capital":
                return self.portfolio_queries.capital_text()
            return self.portfolio_queries.risk_text()
        if normalized in {"/strategies"}:
            raise NotImplementedError(
                "Strategy runtime reporting is not available until the strategy "
                "engine is implemented."
            )
        if normalized in {"/close", "/closeall", "/emergency"}:
            raise NotImplementedError(
                "Position closure is disabled until reconciled execution workflows exist."
            )
        raise ValueError("Unknown command. Use /help.")
