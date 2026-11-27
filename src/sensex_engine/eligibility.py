"""Strategy eligibility gates independent of strategy signal generation."""
from dataclasses import dataclass

from .domain import Regime


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str


def regime_eligibility(regime: Regime, *, chain_valid: bool, liquidity_ok: bool) -> Eligibility:
    if not chain_valid:
        return Eligibility(False, "Option-chain snapshot is invalid")
    if not liquidity_ok:
        return Eligibility(False, "Insufficient option liquidity")
    if regime in {Regime.UNKNOWN, Regime.TRANSITION}:
        return Eligibility(False, "No trade during uncertain regime")
    return Eligibility(True, "Regime and market-quality gates passed")
