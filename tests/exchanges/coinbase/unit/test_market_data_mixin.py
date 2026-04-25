"""Tests for MarketDataMixin — Phase 6, Task 6.2."""

from decimal import Decimal

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.mixins.market_data import MarketDataMixin
from market_connector.testing.mock_transport import MockRestClient


class _TestableMarket(MarketDataMixin):
    def __init__(self, rest):
        self._rest = rest
        self._endpoints = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_get_orderbook_parses_response():
    rest = MockRestClient()
    rest.register(
        "product_book",
        {
            "pricebook": {
                "product_id": "BTC-USD",
                "bids": [{"price": "50000", "size": "0.5"}],
                "asks": [{"price": "50001", "size": "0.3"}],
            },
        },
    )
    mixin = _TestableMarket(rest)
    book = await mixin.get_orderbook("BTC-USD")
    assert book.trading_pair == "BTC-USD"
    assert book.bids[0] == (Decimal("50000"), Decimal("0.5"))


@pytest.mark.asyncio
async def test_get_mid_price_computed_from_book():
    rest = MockRestClient()
    rest.register(
        "product_book",
        {
            "pricebook": {
                "product_id": "BTC-USD",
                "bids": [{"price": "50000", "size": "1"}],
                "asks": [{"price": "50002", "size": "1"}],
            },
        },
    )
    mixin = _TestableMarket(rest)
    assert await mixin.get_mid_price("BTC-USD") == Decimal("50001")


@pytest.mark.asyncio
async def test_get_candles_returns_list():
    rest = MockRestClient()
    rest.register(
        "candles",
        {
            "candles": [
                {
                    "start": "1714000000",
                    "low": "49000",
                    "high": "51000",
                    "open": "50000",
                    "close": "50500",
                    "volume": "10",
                },
            ]
        },
    )
    mixin = _TestableMarket(rest)
    candles = await mixin.get_candles("BTC-USD", "ONE_HOUR", 100)
    assert len(candles) == 1
    assert candles[0][1] == Decimal("50000")  # open


@pytest.mark.asyncio
async def test_get_orderbook_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableMarket(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_orderbook("BTC-USD")


@pytest.mark.asyncio
async def test_get_mid_price_empty_book_returns_zero():
    rest = MockRestClient()
    rest.register(
        "product_book",
        {
            "pricebook": {
                "product_id": "BTC-USD",
                "bids": [],
                "asks": [],
            },
        },
    )
    mixin = _TestableMarket(rest)
    assert await mixin.get_mid_price("BTC-USD") == Decimal("0")


@pytest.mark.asyncio
async def test_get_candles_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableMarket(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_candles("BTC-USD", "ONE_HOUR", 100)
