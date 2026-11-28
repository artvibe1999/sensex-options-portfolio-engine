"""Durable engine position book and equity ledger (SQLite, WAL).

The book records every engine position (paper or live) with its legs, entry
and exit fills, charges, exit reason and thesis/exit plan, plus a per-day
equity row used for daily-loss, drawdown and compounding decisions. Live
positions additionally keep the id of the durable broker position managed by
the fill-confirmed live coordinator, which remains the authority for live
exposure.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS eng_positions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSING','CLOSED','ERROR')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    session_day TEXT NOT NULL,
    lots INTEGER NOT NULL CHECK (lots > 0),
    lot_size INTEGER NOT NULL CHECK (lot_size > 0),
    entry_net TEXT NOT NULL,
    max_loss TEXT NOT NULL,
    max_profit TEXT,
    entry_costs TEXT NOT NULL,
    exit_costs TEXT NOT NULL DEFAULT '0',
    realized_pnl TEXT,
    exit_reason TEXT,
    legs TEXT NOT NULL,
    exit_plan TEXT NOT NULL,
    meta TEXT NOT NULL,
    broker_position_id TEXT
);
CREATE INDEX IF NOT EXISTS eng_positions_status ON eng_positions(status);
CREATE TABLE IF NOT EXISTS eng_equity (
    mode TEXT NOT NULL,
    day TEXT NOT NULL,
    start_equity TEXT NOT NULL,
    peak_equity TEXT NOT NULL,
    halted INTEGER NOT NULL DEFAULT 0,
    entries INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mode, day)
);
CREATE TABLE IF NOT EXISTS eng_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eng_decisions (
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eng_decisions_ts ON eng_decisions(ts);
"""


@dataclass
class BookLeg:
    instrument_token: int
    tradingsymbol: str
    right: str
    strike: float
    expiry: str
    side: str
    quantity: int
    entry_price: float
    entry_iv: float
    exit_price: float | None = None


@dataclass
class BookPosition:
    id: str
    mode: str
    strategy_id: str
    status: str
    opened_at: str
    session_day: str
    lots: int
    lot_size: int
    entry_net: Decimal  # signed premium paid in Rs (credit negative)
    max_loss: Decimal
    max_profit: Decimal | None
    entry_costs: Decimal
    legs: list[BookLeg]
    exit_plan: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    exit_costs: Decimal = Decimal("0")
    realized_pnl: Decimal | None = None
    exit_reason: str | None = None
    closed_at: str | None = None
    broker_position_id: str | None = None

    @property
    def is_credit(self) -> bool:
        return self.entry_net < 0

    def mark_value(self, marks: dict[int, float]) -> float | None:
        """Liquidation value now: longs sold at bid, shorts bought at ask (caller supplies)."""
        total = 0.0
        for leg in self.legs:
            price = marks.get(leg.instrument_token)
            if price is None:
                return None
            total += (1 if leg.side == "BUY" else -1) * leg.quantity * price
        return total

    def unrealized(self, marks: dict[int, float]) -> float | None:
        value = self.mark_value(marks)
        if value is None:
            return None
        return value - float(self.entry_net) - float(self.entry_costs)


class EngineBook:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # executescript() commits implicitly, so run the DDL outside _conn()'s transaction.
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    def health_check(self) -> bool:
        try:
            with self._conn() as conn:
                return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except Exception:
            return False

    # ------------------------------------------------------------- state kv
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM eng_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO eng_state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------ positions
    def open_position(self, position: BookPosition) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO eng_positions(id,mode,strategy_id,status,opened_at,session_day,lots,"
                "lot_size,entry_net,max_loss,max_profit,entry_costs,legs,exit_plan,meta,"
                "broker_position_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    position.id,
                    position.mode,
                    position.strategy_id,
                    position.status,
                    position.opened_at,
                    position.session_day,
                    position.lots,
                    position.lot_size,
                    str(position.entry_net),
                    str(position.max_loss),
                    None if position.max_profit is None else str(position.max_profit),
                    str(position.entry_costs),
                    json.dumps([asdict(x) for x in position.legs]),
                    json.dumps(position.exit_plan),
                    json.dumps(position.meta, default=str),
                    position.broker_position_id,
                ),
            )
            conn.execute(
                "UPDATE eng_equity SET entries = entries + 1 WHERE mode=? AND day=?",
                (position.mode, position.session_day),
            )

    def add_hedge_leg(
        self,
        position_id: str,
        *,
        leg: BookLeg,
        entry_costs: Decimal,
        meta: dict[str, Any],
    ) -> BookPosition:
        """Append the protective short of a leg-in lock and recompute the risk profile.

        net debit = long cost - short credit; max loss = max(net debit, 0) (the short is
        at a further-OTM strike, so the spread can never be worth less than zero); max
        profit = strike width x quantity - net debit.
        """
        position = self.get(position_id)
        if position is None or position.status != "OPEN" or len(position.legs) != 1:
            raise ValueError("Only an open single-leg position can be hedged")
        long_leg = position.legs[0]
        if leg.side != "SELL" or leg.right != long_leg.right or leg.quantity != long_leg.quantity:
            raise ValueError("Hedge must be a same-type, same-quantity short")
        width = abs(Decimal(str(leg.strike)) - Decimal(str(long_leg.strike)))
        entry_net = position.entry_net - Decimal(str(leg.entry_price)) * leg.quantity
        max_loss = max(entry_net, Decimal("0"))
        max_profit = width * leg.quantity - entry_net
        legs = [*position.legs, leg]
        with self._conn() as conn:
            conn.execute(
                "UPDATE eng_positions SET legs=?, entry_net=?, entry_costs=?, max_loss=?, "
                "max_profit=?, meta=? WHERE id=?",
                (
                    json.dumps([asdict(x) for x in legs]),
                    str(entry_net),
                    str(position.entry_costs + entry_costs),
                    str(max_loss),
                    str(max_profit),
                    json.dumps(meta, default=str),
                    position_id,
                ),
            )
        updated = self.get(position_id)
        assert updated is not None
        return updated

    def update_meta(self, position_id: str, meta: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE eng_positions SET meta=? WHERE id=?",
                (json.dumps(meta, default=str), position_id),
            )

    def set_status(self, position_id: str, status: str, reason: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE eng_positions SET status=?, exit_reason=COALESCE(?, exit_reason) "
                "WHERE id=?",
                (status, reason, position_id),
            )

    def close_position(
        self,
        position_id: str,
        *,
        exit_prices: dict[int, float],
        exit_costs: Decimal,
        reason: str,
        closed_at: datetime,
    ) -> Decimal:
        position = self.get(position_id)
        if position is None or position.status == "CLOSED":
            raise ValueError("Position is not open")
        proceeds = Decimal("0")
        for leg in position.legs:
            price = exit_prices.get(leg.instrument_token)
            if price is None:
                raise ValueError(f"Missing exit price for {leg.tradingsymbol}")
            leg.exit_price = price
            sign = Decimal(1) if leg.side == "BUY" else Decimal(-1)
            proceeds += sign * Decimal(str(price)) * leg.quantity
        pnl = (proceeds - position.entry_net - position.entry_costs - exit_costs).quantize(
            Decimal("0.01")
        )
        with self._conn() as conn:
            conn.execute(
                "UPDATE eng_positions SET status='CLOSED', closed_at=?, exit_costs=?, "
                "realized_pnl=?, exit_reason=?, legs=? WHERE id=?",
                (
                    closed_at.isoformat(),
                    str(exit_costs),
                    str(pnl),
                    reason,
                    json.dumps([asdict(x) for x in position.legs]),
                    position_id,
                ),
            )
        return pnl

    def _row(self, row: sqlite3.Row) -> BookPosition:
        return BookPosition(
            id=row["id"],
            mode=row["mode"],
            strategy_id=row["strategy_id"],
            status=row["status"],
            opened_at=row["opened_at"],
            session_day=row["session_day"],
            lots=row["lots"],
            lot_size=row["lot_size"],
            entry_net=Decimal(row["entry_net"]),
            max_loss=Decimal(row["max_loss"]),
            max_profit=None if row["max_profit"] is None else Decimal(row["max_profit"]),
            entry_costs=Decimal(row["entry_costs"]),
            legs=[BookLeg(**x) for x in json.loads(row["legs"])],
            exit_plan=json.loads(row["exit_plan"]),
            meta=json.loads(row["meta"]),
            exit_costs=Decimal(row["exit_costs"]),
            realized_pnl=None if row["realized_pnl"] is None else Decimal(row["realized_pnl"]),
            exit_reason=row["exit_reason"],
            closed_at=row["closed_at"],
            broker_position_id=row["broker_position_id"],
        )

    def get(self, position_id: str) -> BookPosition | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM eng_positions WHERE id=?", (position_id,)).fetchone()
        return self._row(row) if row else None

    def open_positions(self, mode: str | None = None) -> list[BookPosition]:
        query = "SELECT * FROM eng_positions WHERE status IN ('OPEN','CLOSING','ERROR')"
        args: tuple = ()
        if mode:
            query += " AND mode=?"
            args = (mode,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY opened_at", args).fetchall()
        return [self._row(r) for r in rows]

    def closed_positions(self, *, mode: str, since_day: str | None = None) -> list[BookPosition]:
        query = "SELECT * FROM eng_positions WHERE status='CLOSED' AND mode=?"
        args: list[Any] = [mode]
        if since_day:
            query += " AND substr(closed_at, 1, 10)>=?"
            args.append(since_day)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY closed_at", args).fetchall()
        return [self._row(r) for r in rows]

    def realized_total(self, mode: str, *, day: str | None = None) -> Decimal:
        query = "SELECT realized_pnl FROM eng_positions WHERE status='CLOSED' AND mode=?"
        args: list[Any] = [mode]
        if day:  # by close date: a carried position counts on the day it is realised
            query += " AND substr(closed_at, 1, 10)=?"
            args.append(day)
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        return sum((Decimal(r["realized_pnl"]) for r in rows if r["realized_pnl"]), Decimal("0"))

    # --------------------------------------------------------------- equity
    def equity_day(self, mode: str, day: date) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM eng_equity WHERE mode=? AND day=?", (mode, day.isoformat())
            ).fetchone()
        return dict(row) if row else None

    def start_day(
        self, mode: str, day: date, start_equity: Decimal, *, carry_peak: bool = True
    ) -> dict[str, Any]:
        """Create the day's row once (idempotent).

        ``carry_peak`` carries the previous peak forward (paper: equity only changes by
        engine P&L). For live, the account's funds also move with deposits, withdrawals
        and other trading, so the peak restarts each day.
        """
        existing = self.equity_day(mode, day)
        if existing:
            return existing
        with self._conn() as conn:
            prev = conn.execute(
                "SELECT peak_equity FROM eng_equity WHERE mode=? AND day<? "
                "ORDER BY day DESC LIMIT 1",
                (mode, day.isoformat()),
            ).fetchone()
            peak = (
                max(start_equity, Decimal(prev["peak_equity"]))
                if prev and carry_peak
                else start_equity
            )
            conn.execute(
                "INSERT OR IGNORE INTO eng_equity(mode,day,start_equity,peak_equity) "
                "VALUES(?,?,?,?)",
                (mode, day.isoformat(), str(start_equity), str(peak)),
            )
        row = self.equity_day(mode, day)
        assert row is not None
        return row

    def update_peak(self, mode: str, day: date, equity: Decimal) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT peak_equity FROM eng_equity WHERE mode=? AND day=?", (mode, day.isoformat())
            ).fetchone()
            if row and equity > Decimal(row["peak_equity"]):
                conn.execute(
                    "UPDATE eng_equity SET peak_equity=? WHERE mode=? AND day=?",
                    (str(equity), mode, day.isoformat()),
                )

    def reset_equity_reference(self, mode: str, day: date, equity: Decimal) -> None:
        """Start a fresh equity reference (operator capital reset).

        Earlier daily rows only serve as peak/week/month references; they are removed so
        drawdown is measured from the new capital. Today's entry count and halt flag are
        kept. Closed trades (P&L history) are untouched.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM eng_equity WHERE mode=? AND day<?", (mode, day.isoformat()))
            conn.execute(
                "INSERT INTO eng_equity(mode,day,start_equity,peak_equity) VALUES(?,?,?,?) "
                "ON CONFLICT(mode,day) DO UPDATE SET start_equity=excluded.start_equity, "
                "peak_equity=excluded.peak_equity",
                (mode, day.isoformat(), str(equity), str(equity)),
            )

    def set_halted(self, mode: str, day: date, halted: bool = True) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE eng_equity SET halted=? WHERE mode=? AND day=?",
                (1 if halted else 0, mode, day.isoformat()),
            )

    def period_reference(self, mode: str, since: date) -> Decimal | None:
        """Highest start/peak equity recorded since ``since`` (week/month reference)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT start_equity, peak_equity FROM eng_equity WHERE mode=? AND day>=?",
                (mode, since.isoformat()),
            ).fetchall()
        values = [Decimal(r["peak_equity"]) for r in rows] + [
            Decimal(r["start_equity"]) for r in rows
        ]
        return max(values) if values else None

    # ------------------------------------------------------------ decisions
    def log_decision(self, kind: str, body: dict[str, Any], ts: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO eng_decisions(ts,kind,body) VALUES(?,?,?)",
                (ts.isoformat(), kind, json.dumps(body, default=str)),
            )
            conn.execute(
                "DELETE FROM eng_decisions WHERE ts < date(?, '-30 days')", (ts.isoformat(),)
            )

    def recent_decisions(self, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM eng_decisions", []
        if kind:
            query += " WHERE kind=?"
            args.append(kind)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"], **json.loads(r["body"])} for r in rows]


def new_position_id() -> str:
    return uuid4().hex[:16]
