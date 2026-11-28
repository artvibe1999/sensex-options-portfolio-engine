"""Telegram command policy layer; transport handlers can delegate here."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .mode import ModeController
from .telegram_auth import TelegramAllowlist

# Single source of truth for the Telegram command menu (setMyCommands) and /help.
# (command, menu description <= 256 chars, detailed help line)
COMMAND_HELP: tuple[tuple[str, str, str], ...] = (
    ("help", "List all commands with details", "Show this list."),
    (
        "status",
        "Mode, market state, regime, capital tier, last decision",
        "Mode (PAPER/LIVE), live-gate state, SENSEX spot, regime, ADX/RSI/VWAP/ATM IV, capital "
        "tier and risk budget, latest arbitration result and any warning.",
    ),
    (
        "strategies",
        "Why each of the 8 strategies is or isn't eligible now",
        "Per-strategy result on the last completed 5-min bar: ELIGIBLE with score, risk/lot and "
        "legs, or the exact failed rules / rejection reason.",
    ),
    (
        "positions",
        "Open engine positions with legs and live P&L",
        "Open paper and live positions: id, strategy, lots, P&L at bid/ask, max loss, legs.",
    ),
    (
        "pnl",
        "Today's and all-time realised P&L, win rate, profit factor",
        "Realised P&L after charges for today and all time, per mode.",
    ),
    (
        "capital",
        "Equity, start-of-day, peak, open risk, margin room",
        "Dynamic equity (paper compounds; live = Kite funds + engine P&L) and the capital-tier "
        "risk policy in force.",
    ),
    (
        "risk",
        "Same as /capital: risk budget, drawdown, stops",
        "Capital-tier risk per trade, aggregate cap, daily stop, drawdown scaling.",
    ),
    (
        "orders",
        "Durable live order ledger",
        "Live broker orders recorded by the execution ledger (paper fills are not orders).",
    ),
    (
        "pause",
        "Stop new entries (paper and live)",
        "No new entries; open positions are still managed (targets, stops, 15:10 square-off).",
    ),
    (
        "resume",
        "Resume entries; also checks live readiness gates",
        "Paper entries resume immediately. Live gates open only if Kite auth, contracts, fresh "
        "quotes, reconciliation and risk checks all pass (reason shown if not).",
    ),
    (
        "paper",
        "Switch to PAPER mode and pause live gates",
        "Forces PAPER. Existing live positions are still managed and exited.",
    ),
    (
        "live",
        "Request LIVE mode (needs /confirm_live)",
        "Step 1 of 2. Requires a successful /resume first. Does not activate by itself.",
    ),
    (
        "confirm_live",
        "Confirm LIVE mode after /live",
        "Step 2 of 2. Re-checks every readiness gate, then real orders are placed for new "
        "entries. Every restart returns to PAPER.",
    ),
    (
        "close",
        "Close one position: /close <id> confirm",
        "Controlled close of one position from /positions, e.g. /close 9aae614db54c4d5c confirm.",
    ),
    (
        "closeall",
        "Emergency: halt entries and close everything",
        "Pauses entries, forces PAPER, cancels pending live entries, closes live positions "
        "(shorts first) with broker confirmation, and closes paper positions.",
    ),
    ("emergency", "Same as /closeall", "Alias of /closeall."),
    (
        "setcapital",
        "Set paper capital: /setcapital 200000",
        "Set current PAPER equity to any amount, e.g. /setcapital 200000, /setcapital 2.5L, "
        "/setcapital 50k, /setcapital 1cr. Paper only; LIVE capital is always read from the "
        "Zerodha account's available funds. Not allowed while paper positions are open.",
    ),
)


def parse_amount(text: str) -> Decimal | None:
    """Parse rupee amounts like 200000, 2,00,000, Rs 50000, 50k, 2.5L, 2.5 lakh, 1cr."""
    raw = text.strip().lower().replace(",", "").replace("rs", "").replace("₹", "").strip()
    multipliers = (
        ("crore", 10**7),
        ("cr", 10**7),
        ("lakhs", 10**5),
        ("lakh", 10**5),
        ("lac", 10**5),
        ("l", 10**5),
        ("k", 10**3),
    )
    factor = 1
    for suffix, value in multipliers:
        if raw.endswith(suffix):
            raw, factor = raw[: -len(suffix)].strip(), value
            break
    try:
        amount = Decimal(raw) * factor
    except (InvalidOperation, ValueError):
        return None
    # Reject non-positive and absurdly large values (also avoids quantize overflow).
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1e12"):
        return None
    return amount.quantize(Decimal("0.01"))


def help_text() -> str:
    lines = ["SENSEX Hedge Engine commands", ""]
    lines += [f"/{name} - {detail}" for name, _, detail in COMMAND_HELP]
    lines += [
        "",
        "PAPER is the default on every start. LIVE: /resume -> /live -> /confirm_live. "
        "Entries 09:30-14:30 IST, intraday square-off 15:10.",
    ]
    return "\n".join(lines)


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
                return self.engine.status_text()
            return text
        if normalized in {"/help", "/start"}:
            return help_text()
        if normalized == "/paper":
            self.mode.force_paper()
            self.paused = True
            return "Paper mode active; entries remain paused until /resume."
        if normalized == "/live":
            self.mode.request_live(user_id, authorized=True)
            return (
                "Live activation requested. Send /confirm_live separately after reviewing status."
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
            return (
                "Paper entries resumed. Live gates remain closed: startup recovery, broker "
                f"readiness or risk checks did not pass{reason}."
            )
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
            if normalized == "/risk":
                return self.engine.risk_text()
            return self.engine.capital_text()
        if normalized == "/strategies" and self.engine is not None:
            return self.engine.strategies_text()
        if normalized == "/setcapital" and self.engine is not None:
            parts = command.split(maxsplit=1)
            if len(parts) != 2:
                return (
                    "Usage: /setcapital <amount>\n"
                    "Examples: /setcapital 200000, /setcapital 2.5L, /setcapital 50k"
                )
            amount = parse_amount(parts[1])
            if amount is None:
                return f"Could not read '{parts[1][:30]}' as an amount. Example: /setcapital 200000"
            return self.engine.set_paper_capital(amount, user_id=user_id)
        if normalized == "/close" and self.engine is not None:
            parts = command.split()
            if len(parts) != 3 or parts[2].lower() != "confirm":
                return "Usage: /close <position_id> confirm  (see /positions for ids)"
            return self.engine.close_one(parts[1], user_id=user_id)
        if normalized in {"/positions", "/orders"}:
            if self.portfolio_queries is None:
                raise NotImplementedError("Read-only portfolio ledger services are not configured.")
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
