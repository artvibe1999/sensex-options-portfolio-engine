"""Telegram command policy layer; transport handlers can delegate here."""
from dataclasses import dataclass

from .domain import TradingMode
from .mode import ModeController
from .telegram_auth import TelegramAllowlist


@dataclass
class OperationsController:
    allowlist: TelegramAllowlist
    mode: ModeController
    paused: bool = True

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
        if normalized in {"/status", "/help"}:
            return f"mode={self.mode.mode.value}; paused={self.paused}"
        if normalized == "/paper":
            self.mode.force_paper()
            self.paused = True
            return "Paper mode active; entries remain paused until /resume."
        if normalized == "/live":
            self.mode.request_live(user_id, authorized=True)
            return (\n                "Live activation requested. Send /confirm_live separately " \n                "after reviewing status."\n            )
        if normalized == "/confirm_live":
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
            if not broker_ready or not risk_clear:
                raise RuntimeError("Cannot resume: broker readiness or risk checks failed")
            self.paused = False
            return "Entries resumed after readiness checks."
        if normalized in {"/positions", "/orders", "/pnl", "/capital", "/risk", "/strategies"}:
            raise NotImplementedError(\n                "Read-only portfolio query requires the persistence and broker services."\n            )
        if normalized in {"/close", "/closeall", "/emergency"}:
            raise NotImplementedError(\n                "Position closure is disabled until reconciled execution workflows exist."\n            )
        raise ValueError("Unknown command. Use /help.")
