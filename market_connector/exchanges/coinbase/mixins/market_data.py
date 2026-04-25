"""MarketDataMixin: order book, mid-price, and candles via Coinbase REST API."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.converters import (
    to_candle,
    to_exchange_pair,
    to_orderbook_snapshot,
)
from market_connector.exchanges.coinbase.schemas.rest import (
    GetProductCandlesResponse,
    OrderBookResponse,
)

if TYPE_CHECKING:
    from market_connector.exchanges.coinbase.mixins.protocols import HasReady, HasRest
    from market_connector.primitives import OrderBookSnapshot


class MarketDataMixin:
    async def get_orderbook(self: HasRest & HasReady, trading_pair: str) -> OrderBookSnapshot:  # type: ignore[valid-type]
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        product_id = to_exchange_pair(trading_pair)
        raw = await self._rest.request("product_book", params={"product_id": product_id})
        return to_orderbook_snapshot(OrderBookResponse.model_validate(raw))

    async def get_mid_price(self: HasRest & HasReady, trading_pair: str) -> Decimal:  # type: ignore[valid-type]
        book = await self.get_orderbook(trading_pair)
        if not book.bids or not book.asks:
            return Decimal("0")
        bid: Decimal = book.bids[0][0]
        ask: Decimal = book.asks[0][0]
        return (bid + ask) / 2

    async def get_candles(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        interval: str,
        limit: int,
    ) -> list[Any]:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        product_id = to_exchange_pair(trading_pair)
        raw = await self._rest.request(
            "candles",
            params={"product_id": product_id, "granularity": interval, "limit": limit},
        )
        response = GetProductCandlesResponse.model_validate(raw)
        return [to_candle(c) for c in response.candles]
