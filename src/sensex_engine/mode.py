"""Runtime mode state. Process restart always resets to paper."""
from dataclasses import dataclass
from datetime import UTC, datetime

from .domain import TradingMode


@dataclass
class ModeController:
    mode: TradingMode = TradingMode.PAPER
    live_requested_by: int | None = None
    live_confirmed_by: int | None = None
    live_activated_at: datetime | None = None

    def request_live(self, user_id: int, *, authorized: bool) -> None:
        if not authorized:
            raise PermissionError("Telegram user is not authorized")
        if self.mode is TradingMode.LIVE:
            raise RuntimeError("Already in live mode; switch to paper before requesting again")
        self.live_requested_by = user_id
        self.live_confirmed_by = None

    def confirm_live(
        self,
        user_id: int,
        *,
        authorized: bool,
        broker_ready: bool,
        risk_clear: bool,
    ) -> None:
        if not authorized or self.live_requested_by != user_id:
            raise PermissionError("Live confirmation must match the authorized requester")
        if not broker_ready or not risk_clear:
            raise RuntimeError("Live activation checks failed")
        self.mode = TradingMode.LIVE
        self.live_confirmed_by = user_id
        self.live_activated_at = datetime.now(UTC)

    def force_paper(self) -> None:
        self.mode = TradingMode.PAPER
        self.live_requested_by = None
        self.live_confirmed_by = None
        self.live_activated_at = None
