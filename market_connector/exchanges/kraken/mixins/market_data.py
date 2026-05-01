"""MarketDataMixin: order book, ticker, asset pairs, and server time via Kraken REST API."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.errors import raise_on_kraken_error
from market_connector.exchanges.kraken.schemas.rest import (
    AssetPairInfo,
    DepthResult,
    KrakenResponse,
    TickerInfo,
    TickerResult,
    TimeResult,
)
from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER
from market_connector.primitives import OrderBookSnapshot

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasRest


class MarketDataMixin:
    async def get_orderbook(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        depth: int = 25,
    ) -> OrderBookSnapshot:
        """Order book snapshot for *trading_pair*.

        *trading_pair* may be either a Hummingbot canonical pair (``BTC-USD``)
        or an exchange-native pair (``XXBTZUSD``).  When a dash is present the
        mapper normalises it to the exchange format before sending the request.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        raw_pair = (
            KRAKEN_SYMBOL_MAPPER.to_exchange_pair(trading_pair)
            if "-" in trading_pair
            else trading_pair
        )
        response = await self._rest.request(
            "depth",
            params={"pair": raw_pair, "count": depth},
        )
        envelope = KrakenResponse[dict[str, DepthResult]].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        result: dict[str, DepthResult] = envelope.result or {}
        depth_data: DepthResult = next(iter(result.values()))
        bids = [(Decimal(entry[0]), Decimal(entry[1])) for entry in depth_data.bids]
        asks = [(Decimal(entry[0]), Decimal(entry[1])) for entry in depth_data.asks]
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

    async def get_ticker(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
    ) -> dict[str, Any]:
        """Raw Kraken ticker for *trading_pair*.

        Returns the first pair's :class:`TickerInfo` as a plain dict so callers
        can access the raw ``a``/``b``/``c`` arrays without importing the schema.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        raw_pair = (
            KRAKEN_SYMBOL_MAPPER.to_exchange_pair(trading_pair)
            if "-" in trading_pair
            else trading_pair
        )
        response = await self._rest.request("ticker", params={"pair": raw_pair})
        envelope = KrakenResponse[TickerResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        result: TickerResult = envelope.result or {}
        ticker_info: TickerInfo = next(iter(result.values()))
        return ticker_info.model_dump()

    async def get_mid_price(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
    ) -> Decimal:
        """Computed ``(bid + ask) / 2`` from the live ticker."""
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        raw_pair = (
            KRAKEN_SYMBOL_MAPPER.to_exchange_pair(trading_pair)
            if "-" in trading_pair
            else trading_pair
        )
        response = await self._rest.request("ticker", params={"pair": raw_pair})
        envelope = KrakenResponse[TickerResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        result: TickerResult = envelope.result or {}
        ticker_info: TickerInfo = next(iter(result.values()))
        bid = Decimal(ticker_info.b[0])
        ask = Decimal(ticker_info.a[0])
        return (bid + ask) / 2

    async def get_asset_pairs(
        self: HasRest & HasReady,  # type: ignore[valid-type]
    ) -> dict[str, AssetPairInfo]:
        """Full asset pair registry from ``/0/public/AssetPairs``.

        Used by the symbol mapper initialisation in Stage 5.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        response = await self._rest.request("asset_pairs")
        envelope = KrakenResponse[dict[str, AssetPairInfo]].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        return envelope.result or {}

    async def get_server_time(
        self: HasRest & HasReady,  # type: ignore[valid-type]
    ) -> int:
        """Unix timestamp from ``/0/public/Time``."""
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        response = await self._rest.request("server_time")
        envelope = KrakenResponse[TimeResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        assert envelope.result is not None  # noqa: S101 – guarded by raise_on_kraken_error
        return envelope.result.unixtime
