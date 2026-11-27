"""Read-only Zerodha Kite Connect adapter for BFO SENSEX option data.

Order placement is intentionally not exposed by this adapter.
"""
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .domain import Contract, OptionLeg, Right
from .market_data import Quote

IST = ZoneInfo("Asia/Kolkata")


class KiteReadOnlyAdapter:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._symbols_by_token: dict[int, str] = {}

    @classmethod
    def from_credentials(cls, api_key: str, access_token: str) -> "KiteReadOnlyAdapter":
        if not api_key.strip() or not access_token.strip():
            raise ValueError("Kite API key and access token are required")
        try:
            from kiteconnect import KiteConnect
        except ImportError as exc:
            raise RuntimeError(
                "Install the optional broker dependency with pip install '.[broker]'"
            ) from exc
        client = KiteConnect(api_key=api_key)
        client.set_access_token(access_token)
        return cls(client)

    def is_authenticated(self) -> bool:
        try:
            self._client.profile()
            return True
        except Exception:
            return False

    def instruments(self) -> list[Contract]:
        # Replace, do not merge, the cache so delisted/expired contracts cannot linger.
        self._symbols_by_token.clear()
        rows = self._client.instruments("BFO")
        contracts: list[Contract] = []
        for row in rows:
            if row.get("name") != "SENSEX":
                continue
            instrument_type = row.get("instrument_type")
            if instrument_type not in {"CE", "PE"}:
                continue
            expiry_value = row.get("expiry")
            if isinstance(expiry_value, datetime):
                expiry = (
                    expiry_value.replace(tzinfo=IST)
                    if expiry_value.tzinfo is None
                    else expiry_value.astimezone(IST)
                )
            elif isinstance(expiry_value, date):
                expiry = datetime.combine(expiry_value, time.min, tzinfo=IST)
            else:
                continue
            token = int(row["instrument_token"])
            contract = Contract(
                tradingsymbol=str(row["tradingsymbol"]),
                instrument_token=token,
                expiry=expiry,
                strike=Decimal(str(row["strike"])),
                right=Right(instrument_type),
                lot_size=int(row["lot_size"]),
                tick_size=Decimal(str(row["tick_size"])),
            )
            contracts.append(contract)
            self._symbols_by_token[token] = f"BFO:{contract.tradingsymbol}"
        return contracts

    def quotes(self, instrument_tokens: list[int]) -> dict[int, Quote]:
        if not instrument_tokens:
            return {}
        missing = [token for token in instrument_tokens if token not in self._symbols_by_token]
        if missing:
            raise ValueError("Refresh the BFO contract master before requesting unknown tokens")
        raw_quotes = self._client.quote(
            [self._symbols_by_token[token] for token in instrument_tokens]
        )
        result: dict[int, Quote] = {}
        for token in instrument_tokens:
            raw = raw_quotes.get(self._symbols_by_token[token])
            if not raw:
                continue
            depth = raw.get("depth") or {}
            buy = (depth.get("buy") or [{}])[0]
            sell = (depth.get("sell") or [{}])[0]
            timestamp = raw.get("timestamp") or raw.get("last_trade_time")
            if timestamp is None:
                continue
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if not isinstance(timestamp, datetime):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=IST)
            timestamp = timestamp.astimezone(UTC)
            result[token] = Quote(
                instrument_token=token,
                timestamp=timestamp,
                last_price=Decimal(str(raw.get("last_price", 0))),
                bid=Decimal(str(buy.get("price", 0))),
                ask=Decimal(str(sell.get("price", 0))),
                bid_quantity=int(buy.get("quantity", 0)),
                ask_quantity=int(sell.get("quantity", 0)),
                volume=int(raw.get("volume", 0)),
            )
        return result

    def margins(self, segment: str = "equity") -> dict[str, Any]:
        """Return authenticated broker funds/margins for the requested segment."""
        if segment not in {"equity", "commodity"}:
            raise ValueError("Kite margin segment must be equity or commodity")
        result = self._client.margins(segment)
        if not isinstance(result, dict):
            raise ValueError("Kite margin response must be an object")
        return result

    def basket_margin(
        self, legs: tuple[OptionLeg, ...], prices: dict[int, Decimal],
    ) -> Decimal:
        """Use the broker's initial and final basket requirements."""
        orders = [
            {
                "exchange": "BFO", "tradingsymbol": leg.contract.tradingsymbol,
                "transaction_type": leg.side.value, "variety": "regular",
                "product": "NRML", "order_type": "LIMIT",
                "quantity": leg.lots * leg.contract.lot_size,
                "price": float(prices[leg.contract.instrument_token]),
                "trigger_price": 0,
            }
            for leg in sorted(legs, key=lambda x: x.side.value != "BUY")
        ]
        response = self._client.basket_order_margins(orders, consider_positions=True)
        initial = Decimal(str(response["initial"]["total"]))
        final = Decimal(str(response["final"]["total"]))
        if not all(x.is_finite() and x >= 0 for x in (initial, final)):
            raise ValueError("Invalid Kite basket margin")
        result = max(initial, final)
        if result <= 0:
            raise ValueError("Positive basket margin is required")
        return result

    def positions(self) -> dict[str, Any]:
        return self._client.positions()

    def orders(self) -> list[dict[str, Any]]:
        return self._client.orders()


    def order_trades(self, broker_order_id: str) -> list[dict[str, Any]]:
        """Return individual executions for one current-session broker order.

        Kite's order/trade history is transient and should be backfilled into the
        local durable fill inbox during the trading session.
        """
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("Broker order ID is required")
        rows = self._client.order_trades(broker_order_id)
        if not isinstance(rows, list):
            raise ValueError("Kite order_trades response must be a list")
        return rows
