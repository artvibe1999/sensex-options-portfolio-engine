from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from sensex_engine.kite import KiteReadOnlyAdapter


class FakeKite:
    def __init__(self):
        self.rows = [
            {
                "name": "SENSEX",
                "instrument_type": "CE",
                "expiry": date(2026, 12, 1),
                "instrument_token": 987,
                "tradingsymbol": "SENSEX26DEZ80000CE",
                "strike": 80000,
                "lot_size": 20,
                "tick_size": 0.05,
            },
            {
                "name": "BANKEX",
                "instrument_type": "CE",
                "expiry": date(2026, 12, 1),
                "instrument_token": 988,
                "tradingsymbol": "BANKEX26DEC",
                "strike": 60000,
                "lot_size": 15,
                "tick_size": 0.05,
            },
        ]

    def instruments(self, exchange):
        assert exchange == "BFO"
        return self.rows

    def profile(self):
        return {"user_id": "TEST"}

    def quote(self, symbols):
        return {
            symbols[0]: {
                "timestamp": datetime.now(UTC),
                "last_price": 100,
                "volume": 1200,
                "depth": {
                    "buy": [{"price": 99.9, "quantity": 50}],
                    "sell": [{"price": 100.1, "quantity": 60}],
                },
            }
        }


def test_kite_adapter_filters_sensex_options_and_maps_quotes():
    adapter = KiteReadOnlyAdapter(FakeKite())
    contracts = adapter.instruments()
    assert len(contracts) == 1
    assert contracts[0].instrument_token == 987
    assert contracts[0].lot_size == 20
    quote = adapter.quotes([987])[987]
    assert quote.bid == Decimal("99.9")
    assert quote.ask == Decimal("100.1")
    assert quote.volume == 1200


def test_kite_quote_requires_current_contract_master():
    adapter = KiteReadOnlyAdapter(FakeKite())
    with pytest.raises(ValueError):
        adapter.quotes([987])


def test_kite_authentication_fails_closed():
    class Unauthenticated(FakeKite):
        def profile(self):
            raise RuntimeError("expired token")

    assert not KiteReadOnlyAdapter(Unauthenticated()).is_authenticated()


def test_empty_kite_credentials_rejected():
    with pytest.raises(ValueError):
        KiteReadOnlyAdapter.from_credentials("", "token")

def test_contract_refresh_drops_expired_or_removed_symbols():
    fake = FakeKite()
    adapter = KiteReadOnlyAdapter(fake)
    adapter.instruments()
    fake.rows = []
    assert adapter.instruments() == []
    with pytest.raises(ValueError):
        adapter.quotes([987])


def test_kite_adapter_parses_iso_quote_timestamp_and_naive_expiry_as_ist():
    fake = FakeKite()
    fake.rows[0]["expiry"] = datetime(2026, 12, 1)
    original_quote = fake.quote

    def quote(symbols):
        payload = original_quote(symbols)
        payload[symbols[0]]["timestamp"] = datetime.now(UTC).isoformat()
        return payload

    fake.quote = quote
    adapter = KiteReadOnlyAdapter(fake)
    contract = adapter.instruments()[0]
    assert contract.expiry.tzinfo is not None
    assert contract.expiry.utcoffset().total_seconds() == 19800
    assert adapter.quotes([987])[987].timestamp.tzinfo is not None


def test_kite_adapter_skips_malformed_quote_timestamps():
    fake = FakeKite()
    original_quote = fake.quote

    def quote(symbols):
        payload = original_quote(symbols)
        payload[symbols[0]]["timestamp"] = "not-a-timestamp"
        return payload

    fake.quote = quote
    adapter = KiteReadOnlyAdapter(fake)
    adapter.instruments()
    assert adapter.quotes([987]) == {}


def test_kite_adapter_exposes_read_only_per_order_trade_history():
    class WithTrades(FakeKite):
        def __init__(self):
            super().__init__()
            self.trade_order_ids = []

        def order_trades(self, order_id):
            self.trade_order_ids.append(order_id)
            return [{"trade_id": "t1", "order_id": order_id}]

    fake = WithTrades()
    adapter = KiteReadOnlyAdapter(fake)
    assert adapter.order_trades("kite-1") == [
        {"trade_id": "t1", "order_id": "kite-1"}
    ]
    assert fake.trade_order_ids == ["kite-1"]
    with pytest.raises(ValueError):
        adapter.order_trades("")



def test_kite_adapter_exposes_equity_margin_snapshot():
    adapter = KiteReadOnlyAdapter(FakeKite())
    result = adapter.margins("equity")
    assert result["enabled"] is True
    assert result["net"] == 100000
    with pytest.raises(ValueError, match="segment"):
        adapter.margins("bfo")
