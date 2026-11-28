from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sensex_engine.domain import Contract, Right
from sensex_engine.filled_risk import filled_position_risk


def test_partial_fills_use_units_not_requested_lots():
    expiry = datetime(2026, 12, 3, tzinfo=UTC)
    contracts = [
        Contract("A", 1, expiry, Decimal("80000"), Right.CE, 20, Decimal(".05")),
        Contract("B", 2, expiry, Decimal("80100"), Right.CE, 20, Decimal(".05")),
    ]
    legs = [
        dict(instrument_token=1, tradingsymbol="A", side="BUY",
             remaining_quantity=7, open_average_price="100"),
        dict(instrument_token=2, tradingsymbol="B", side="SELL",
             remaining_quantity=7, open_average_price="60"),
    ]
    positions = [dict(mode="live", legs=legs)]
    assert filled_position_risk(positions, contracts, mode="live") == (Decimal("280"), 1)
    assert filled_position_risk(positions, contracts, mode="paper") == (Decimal("0"), 0)
    legs[0]["remaining_quantity"] = 3
    with pytest.raises(ValueError, match="Unbounded"):
        filled_position_risk(positions, contracts, mode="live")
    legs[1]["remaining_quantity"] = 0
    assert filled_position_risk(positions, contracts, mode="live") == (Decimal("300"), 1)
