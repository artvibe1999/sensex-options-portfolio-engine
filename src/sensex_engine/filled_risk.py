"""Conservative exposure risk from actual durable fills, including odd quantities."""
from decimal import Decimal
from typing import Any

from .domain import Contract, Right


def filled_position_risk(
    positions: list[dict[str, Any]], contracts: list[Contract], *, mode: str
) -> tuple[Decimal, int]:
    """Return aggregate entry-basis loss and exposed position count.

    Cross-expiry structures require a separate validated risk model.
    No offsets are granted across independent positions.
    """
    master = {contract.instrument_token: contract for contract in contracts}
    total = Decimal("0")
    count = 0
    for position in positions:
        if position["mode"] != mode:
            continue
        legs = []
        for leg in position["legs"]:
            quantity = leg["remaining_quantity"]
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise ValueError("Invalid remaining quantity")
            if quantity == 0:
                continue
            contract = master.get(leg["instrument_token"])
            if contract is None or contract.tradingsymbol != leg["tradingsymbol"]:
                raise ValueError("Missing or mismatched contract metadata")
            premium = Decimal(str(leg["open_average_price"]))
            if not premium.is_finite() or premium <= 0:
                raise ValueError("Invalid verified opening premium")
            if leg["side"] not in {"BUY", "SELL"}:
                raise ValueError("Invalid leg side")
            signed = quantity if leg["side"] == "BUY" else -quantity
            legs.append((contract, premium, signed))
        if not legs:
            continue
        count += 1
        if len({contract.expiry for contract, _, _ in legs}) != 1:
            raise ValueError("Cross-expiry exposure requires validated risk model")
        if sum(q for c, _, q in legs if c.right == Right.CE) < 0:
            raise ValueError("Unbounded short call exposure")
        spots = {Decimal("0"), *(c.strike for c, _, _ in legs)}
        def pnl(spot: Decimal, exposure: list = legs) -> Decimal:
            return sum(
                (
                    (max(spot - c.strike if c.right == Right.CE else c.strike - spot,
                         Decimal("0")) - premium) * q
                    for c, premium, q in exposure
                ),
                Decimal("0"),
            )
        total += max(Decimal("0"), -min(pnl(spot) for spot in spots))
    return total, count
