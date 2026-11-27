from datetime import date
from decimal import Decimal

import pytest

from sensex_engine.broker_submission import BrokerOrderStatus
from sensex_engine.execution import LegExecutionAction
from sensex_engine.kite import KiteReadOnlyAdapter
from sensex_engine.kite_execution import KiteOrderExecutionAdapter


class FakeKite:
    def __init__(self):
        self.rows = [{
            "name": "SENSEX",
            "instrument_type": "CE",
            "expiry": date(2026, 12, 1),
            "instrument_token": 987,
            "tradingsymbol": "SENSEX26DEZ80000CE",
            "strike": 80000,
            "lot_size": 20,
            "tick_size": 0.05,
        }]
        self.order_rows = []
        self.place_calls = []
        self.modify_calls = []
        self.cancel_calls = []
        self.history = {}

    def instruments(self, exchange):
        assert exchange == "BFO"
        return self.rows

    def orders(self):
        return list(self.order_rows)

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return "kite-123"

    def order_history(self, order_id):
        return self.history[order_id]

    def modify_order(self, **kwargs):
        self.modify_calls.append(kwargs)
        return kwargs["order_id"]

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return kwargs["order_id"]


def setup_adapter():
    client = FakeKite()
    market_data = KiteReadOnlyAdapter(client)
    adapter = KiteOrderExecutionAdapter(market_data)
    action = LegExecutionAction(
        0, 987, "SENSEX26DEZ80000CE", "BUY", 40, Decimal("100.05")
    )
    return client, adapter, action


def test_kite_order_uses_current_bfo_contract_limit_price_and_20_char_tag():
    client, adapter, action = setup_adapter()
    receipt = adapter.submit_order(action, client_order_key="durable-order-key-1")

    assert receipt.broker_order_id == "kite-123"
    assert receipt.status is BrokerOrderStatus.ACCEPTED
    params = client.place_calls[0]
    assert params["exchange"] == "BFO"
    assert params["tradingsymbol"] == action.tradingsymbol
    assert params["transaction_type"] == "BUY"
    assert params["quantity"] == 40
    assert params["product"] == "NRML"
    assert params["order_type"] == "LIMIT"
    assert params["price"] == 100.05
    assert params["validity"] == "DAY"
    assert len(params["tag"]) == 20
    assert params["tag"].isalnum()


def test_kite_order_rejects_missing_off_tick_price_and_non_lot_quantity():
    client, adapter, action = setup_adapter()
    with pytest.raises(ValueError, match="LIMIT price is mandatory"):
        adapter.submit_order(
            LegExecutionAction(0, 987, action.tradingsymbol, "BUY", 20),
            client_order_key="key-a",
        )
    with pytest.raises(ValueError, match="tick"):
        adapter.submit_order(
            LegExecutionAction(0, 987, action.tradingsymbol, "BUY", 20, Decimal("100.03")),
            client_order_key="key-b",
        )
    with pytest.raises(ValueError, match="whole number of current lots"):
        adapter.submit_order(
            LegExecutionAction(0, 987, action.tradingsymbol, "BUY", 21, Decimal("100.05")),
            client_order_key="key-c",
        )
    assert client.place_calls == []


def test_kite_order_rejects_contract_not_in_refreshed_bfo_master():
    client, adapter, action = setup_adapter()
    with pytest.raises(ValueError, match="not uniquely present"):
        adapter.submit_order(
            LegExecutionAction(999, 999, "OTHER", "BUY", 20, Decimal("100")),
            client_order_key="key",
        )
    assert client.place_calls == []


def test_exact_tag_lookup_maps_order_states_without_treating_ack_as_fill():
    client, adapter, _ = setup_adapter()
    tag = adapter.tag_for_client_key("client-1")
    client.order_rows = [{
        "tag": tag,
        "order_id": "kite-open",
        "status": "OPEN",
        "exchange": "BFO",
        "tradingsymbol": "SENSEX26DEZ80000CE",
        "transaction_type": "BUY",
        "quantity": 20,
    }]
    matches = adapter.find_orders_by_client_key("client-1")
    assert len(matches) == 1
    assert matches[0].broker_order_id == "kite-open"
    assert matches[0].status is BrokerOrderStatus.OPEN


def test_duplicate_tag_matches_are_returned_for_durable_layer_to_block():
    client, adapter, _ = setup_adapter()
    tag = adapter.tag_for_client_key("client-2")
    client.order_rows = [
        {"tag": tag, "order_id": "kite-a", "status": "OPEN", "exchange": "BFO", "tradingsymbol": "SENSEX26DEZ80000CE", "transaction_type": "BUY", "quantity": 20},
        {"tag": tag, "order_id": "kite-b", "status": "COMPLETE", "exchange": "BFO", "tradingsymbol": "SENSEX26DEZ80000CE", "transaction_type": "SELL", "quantity": 20},
    ]
    assert len(adapter.find_orders_by_client_key("client-2")) == 2


def test_submit_does_not_place_again_when_exact_existing_order_is_found():
    client, adapter, action = setup_adapter()
    tag = adapter.tag_for_client_key("same-key")
    client.order_rows = [{
        "tag": tag,
        "order_id": "kite-existing",
        "status": "OPEN",
        "exchange": "BFO",
        "tradingsymbol": action.tradingsymbol,
        "transaction_type": "BUY",
        "quantity": 40,
    }]
    receipt = adapter.submit_order(action, client_order_key="same-key")
    assert receipt.broker_order_id == "kite-existing"
    assert receipt.status is BrokerOrderStatus.OPEN
    assert client.place_calls == []


def test_tag_collision_with_different_order_fields_fails_closed():
    client, adapter, action = setup_adapter()
    tag = adapter.tag_for_client_key("same-key")
    client.order_rows = [{
        "tag": tag,
        "order_id": "kite-other",
        "status": "OPEN",
        "exchange": "BFO",
        "tradingsymbol": action.tradingsymbol,
        "transaction_type": "SELL",
        "quantity": 40,
    }]
    with pytest.raises(RuntimeError, match="different order"):
        adapter.submit_order(action, client_order_key="same-key")
    assert client.place_calls == []


def test_cancel_requires_exact_tag_bfo_sensex_contract_and_open_status():
    client, adapter, action = setup_adapter()
    tag = adapter.tag_for_client_key("cancel-key")
    client.history["kite-open"] = [{
        "order_id": "kite-open",
        "tag": tag,
        "exchange": "BFO",
        "tradingsymbol": action.tradingsymbol,
        "status": "OPEN",
        "variety": "regular",
    }]
    result = adapter.cancel_order("kite-open", client_order_key="cancel-key")
    assert result.status is BrokerOrderStatus.OPEN
    assert "await broker status" in result.message
    assert client.cancel_calls == [{"variety": "regular", "order_id": "kite-open"}]


def test_cancel_refuses_wrong_client_key_or_terminal_order():
    client, adapter, action = setup_adapter()
    tag = adapter.tag_for_client_key("right-key")
    client.history["kite-done"] = [{
        "order_id": "kite-done",
        "tag": tag,
        "exchange": "BFO",
        "tradingsymbol": action.tradingsymbol,
        "status": "COMPLETE",
        "variety": "regular",
    }]
    with pytest.raises(ValueError, match="does not match"):
        adapter.cancel_order("kite-done", client_order_key="wrong-key")
    with pytest.raises(ValueError, match="Only open"):
        adapter.cancel_order("kite-done", client_order_key="right-key")
    assert client.cancel_calls == []


def test_modify_order_checks_tick_size_lot_size_and_filled_quantity():
    client, adapter, action = setup_adapter()
    tag = adapter.tag_for_client_key("modify-key")
    client.history["kite-open"] = [{
        "order_id": "kite-open",
        "tag": tag,
        "exchange": "BFO",
        "tradingsymbol": action.tradingsymbol,
        "status": "OPEN",
        "order_type": "LIMIT",
        "variety": "regular",
        "quantity": 40,
        "filled_quantity": 20,
    }]
    result = adapter.modify_order(
        "kite-open", client_order_key="modify-key",
        price=Decimal("100.10"), quantity=40,
    )
    assert result.status is BrokerOrderStatus.OPEN
    assert client.modify_calls[0]["price"] == 100.1
    with pytest.raises(ValueError, match="below already-filled"):
        adapter.modify_order(
            "kite-open", client_order_key="modify-key",
            price=Decimal("100.10"), quantity=20,
        )
    assert len(client.modify_calls) == 1


def test_unknown_kite_status_is_not_guessed():
    client, adapter, _ = setup_adapter()
    tag = adapter.tag_for_client_key("unknown-status")
    client.order_rows = [{"tag": tag, "order_id": "kite-x", "status": "SOMETHING_NEW", "exchange": "BFO", "tradingsymbol": "SENSEX26DEZ80000CE", "transaction_type": "BUY", "quantity": 20}]
    with pytest.raises(ValueError, match="Unsupported Kite order status"):
        adapter.find_orders_by_client_key("unknown-status")
