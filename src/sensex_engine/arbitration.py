"""Strategy arbitration: one best risk-adjusted addition, or no trade.

Order follows the requirement's priority list (a selection order, not a
profitability ranking); a lower-priority family executes only when every
higher-priority family is ineligible. Portfolio interaction rules:
* never add a second position with the same directional thesis;
* never open a position that conflicts with an open directional position
  (the open one must exit on its own invalidation first);
* never stack two short-premium neutral structures;
* respect the concurrent-position limit of the current capital tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from .strategies import DIRECTION, Evaluation, Proposal

OPPOSITE = {"bullish": "bearish", "bearish": "bullish"}


@dataclass(frozen=True)
class ArbitrationResult:
    selected: Proposal | None
    reason: str
    considered: tuple[str, ...]


def arbitrate(
    evaluations: list[Evaluation],
    *,
    open_strategies: list[str],
    max_concurrent: int,
) -> ArbitrationResult:
    if len(open_strategies) >= max_concurrent:
        return ArbitrationResult(None, f"max concurrent positions ({max_concurrent}) open", ())
    open_dirs = {DIRECTION.get(s, "unknown") for s in open_strategies}
    considered: list[str] = []
    for ev in sorted(evaluations, key=lambda e: e.priority):
        if not ev.eligible:
            continue
        direction = DIRECTION[ev.strategy_id]
        if ev.strategy_id in open_strategies:
            considered.append(f"{ev.strategy_id}: already open")
            continue
        if direction in open_dirs and direction != "volatility":
            considered.append(f"{ev.strategy_id}: same {direction} thesis already open")
            continue
        if OPPOSITE.get(direction) in open_dirs:
            considered.append(
                f"{ev.strategy_id}: conflicts with open {OPPOSITE[direction]} position"
            )
            continue
        if direction == "volatility" and "neutral" in open_dirs:
            considered.append(f"{ev.strategy_id}: conflicts with open short-volatility position")
            continue
        if direction == "neutral" and "volatility" in open_dirs:
            considered.append(f"{ev.strategy_id}: conflicts with open long-volatility position")
            continue
        assert ev.proposal is not None
        return ArbitrationResult(ev.proposal, f"selected priority {ev.priority}", tuple(considered))
    if considered:
        return ArbitrationResult(
            None, "eligible strategies blocked by portfolio rules", tuple(considered)
        )
    return ArbitrationResult(None, "no strategy passed all setup and strike gates", ())
