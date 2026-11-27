from decimal import Decimal

import pytest

from sensex_engine.persistence import SqliteStore
from sensex_engine.position_lifecycle import PositionLifecycle


def make_position(tmp_path):
    lifecycle = PositionLifecycle(SqliteStore(tmp_path / "positions.sqlite3"))
    lifecycle.create(
        position_id="position-1",
        strategy_id="BULL_CALL_DEBIT",
        mode="paper",
        idempotency_key="position-create-1",
        legs=[
            {"leg_index": 0, "instrument_token": 101, "tradingsymbol": "SENSEX_CE_1",
             "side": "BUY", "quantity": 10},
            {"leg_index": 1, "instrument_token": 102, "tradingsymbol": "SENSEX_CE_2",
             "side": "SELL", "quantity": 10},
        ],
    )
    return lifecycle


def fill(lifecycle, *, leg, phase, fill_id, quantity, price="100"):
    return lifecycle.record_leg_fill(
        position_id="position-1", leg_index=leg, phase=phase, fill_id=fill_id,
        quantity=quantity, price=Decimal(price),
        filled_at="2026-11-27T10:00:00+05:30",
    )


def test_position_requires_all_legs_filled_before_open(tmp_path):
    lifecycle = make_position(tmp_path)
    assert fill(lifecycle, leg=0, phase="OPEN", fill_id="o1", quantity=5)
    assert lifecycle.snapshot("position-1")["status"] == "PENDING"
    assert fill(lifecycle, leg=0, phase="OPEN", fill_id="o2", quantity=5)
    assert lifecycle.snapshot("position-1")["status"] == "PENDING"
    assert fill(lifecycle, leg=1, phase="OPEN", fill_id="o3", quantity=10)
    snapshot = lifecycle.snapshot("position-1")
    assert snapshot["status"] == "OPEN"
    assert snapshot["is_fully_open"] is True
    assert [leg["remaining_quantity"] for leg in snapshot["legs"]] == [10, 10]


def test_position_cannot_be_closed_until_every_open_leg_is_filled_closed(tmp_path):
    lifecycle = make_position(tmp_path)
    fill(lifecycle, leg=0, phase="OPEN", fill_id="o1", quantity=10)
    fill(lifecycle, leg=1, phase="OPEN", fill_id="o2", quantity=10)
    lifecycle.transition(position_id="position-1", status="CLOSING", update_id="close")
    assert fill(lifecycle, leg=0, phase="CLOSE", fill_id="c1", quantity=10)
    assert lifecycle.snapshot("position-1")["status"] == "CLOSING"
    assert fill(lifecycle, leg=1, phase="CLOSE", fill_id="c2", quantity=10)
    snapshot = lifecycle.snapshot("position-1")
    assert snapshot["status"] == "CLOSED"
    assert snapshot["is_fully_closed"] is True
    assert [leg["remaining_quantity"] for leg in snapshot["legs"]] == [0, 0]


def test_position_rejects_closing_more_than_confirmed_open_quantity(tmp_path):
    lifecycle = make_position(tmp_path)
    fill(lifecycle, leg=0, phase="OPEN", fill_id="o1", quantity=5)
    lifecycle.transition(position_id="position-1", status="CLOSING", update_id="abort")
    with pytest.raises(ValueError, match="exceed confirmed open"):
        fill(lifecycle, leg=0, phase="CLOSE", fill_id="c1", quantity=6)


def test_position_cannot_be_manually_marked_closed(tmp_path):
    lifecycle = make_position(tmp_path)
    lifecycle.transition(position_id="position-1", status="CLOSING", update_id="close")
    with pytest.raises(ValueError, match="Invalid position transition"):
        lifecycle.transition(position_id="position-1", status="CLOSED", update_id="fake-close")


def test_position_fill_is_idempotent_and_conflict_checked(tmp_path):
    lifecycle = make_position(tmp_path)
    assert fill(lifecycle, leg=0, phase="OPEN", fill_id="same", quantity=5)
    assert not fill(lifecycle, leg=0, phase="OPEN", fill_id="same", quantity=5)
    with pytest.raises(ValueError, match="conflicts"):
        fill(lifecycle, leg=0, phase="OPEN", fill_id="same", quantity=4)


def test_position_lifecycle_reconstructs_after_restart(tmp_path):
    lifecycle = make_position(tmp_path)
    fill(lifecycle, leg=0, phase="OPEN", fill_id="o1", quantity=10, price="99")
    fill(lifecycle, leg=1, phase="OPEN", fill_id="o2", quantity=10, price="101")
    restarted = PositionLifecycle(SqliteStore(tmp_path / "positions.sqlite3"))
    snapshot = restarted.snapshot("position-1")
    assert snapshot["status"] == "OPEN"
    assert snapshot["legs"][0]["open_average_price"] == "99"
    assert snapshot["legs"][1]["open_average_price"] == "101"


def test_position_rejects_duplicate_leg_indexes_and_bad_contracts(tmp_path):
    lifecycle = PositionLifecycle(SqliteStore(tmp_path / "positions.sqlite3"))
    with pytest.raises(ValueError, match="invalid or duplicate leg"):
        lifecycle.create(
            position_id="bad", strategy_id="x", mode="paper", idempotency_key="bad",
            legs=[
                {"leg_index": 0, "instrument_token": 1, "tradingsymbol": "A",
                 "side": "BUY", "quantity": 1},
                {"leg_index": 0, "instrument_token": 2, "tradingsymbol": "B",
                 "side": "SELL", "quantity": 1},
            ],
        )



def test_late_open_fill_after_flattening_reopens_position_as_closing(tmp_path):
    lifecycle = make_position(tmp_path)
    fill(lifecycle, leg=0, phase="OPEN", fill_id="o1", quantity=10)
    fill(lifecycle, leg=1, phase="OPEN", fill_id="o2", quantity=10)
    lifecycle.transition(position_id="position-1", status="CLOSING", update_id="close")
    fill(lifecycle, leg=1, phase="CLOSE", fill_id="c1", quantity=10)
    fill(lifecycle, leg=0, phase="CLOSE", fill_id="c2", quantity=10)
    assert lifecycle.snapshot("position-1")["status"] == "CLOSED"

    # A trade-history update can arrive after the cancel/close sequence was
    # reconciled. Re-open the durable exposure state rather than dropping it.
    fill(lifecycle, leg=0, phase="OPEN", fill_id="late-open", quantity=5)
    snapshot = lifecycle.snapshot("position-1")
    assert snapshot["status"] == "CLOSING"
    assert snapshot["legs"][0]["remaining_quantity"] == 5
    assert snapshot["is_fully_closed"] is False
