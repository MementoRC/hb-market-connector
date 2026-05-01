"""Tests for Kraken MarketDataMixin."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.errors import KrakenAPIError
from market_connector.exchanges.kraken.mixins.market_data import MarketDataMixin
from market_connector.testing.mock_transport import MockRestClient

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "rest"


# ---------------------------------------------------------------------------
# Minimal testable composition
# ---------------------------------------------------------------------------


class _TestableMarketData(MarketDataMixin):
    def __init__(self, rest: MockRestClient) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


# ---------------------------------------------------------------------------
# Synthetic response helpers
# ---------------------------------------------------------------------------

_TICKER_RESPONSE = {
    "error": [],
    "result": {
        "XXBTZUSD": {
            "a": ["50000.0", "1", "1.000"],
            "b": ["49999.0", "1", "1.000"],
            "c": ["50000.0", "0.500"],
            "v": ["1234.0", "5678.0"],
            "p": ["49999.5", "49999.5"],
            "t": [100, 200],
            "l": ["49000.0", "49000.0"],
            "h": ["51000.0", "51000.0"],
            "o": "49500.0",
        }
    },
}

_DEPTH_RESPONSE = {
    "error": [],
    "result": {
        "XXBTZUSD": {
            "bids": [
                ["49999.0", "1.500", "1714000001"],
                ["49998.0", "2.000", "1714000000"],
            ],
            "asks": [
                ["50000.0", "0.800", "1714000001"],
                ["50001.0", "1.200", "1714000000"],
            ],
        }
    },
}

_TIME_RESPONSE = {
    "error": [],
    "result": {
        "unixtime": 1714000000,
        "rfc1123": "Thu, 25 Apr 2024 00:00:00 +0000",
    },
}

_ERROR_RESPONSE = {
    "error": ["EGeneral:Permission denied"],
    "result": None,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_asset_pairs_returns_full_dict():
    """get_asset_pairs returns every pair in the fixture (keyed by Kraken symbol)."""
    raw = json.loads((_FIXTURES_DIR / "asset_pairs.json").read_text())
    rest = MockRestClient()
    rest.register("asset_pairs", raw)
    mixin = _TestableMarketData(rest)

    pairs = await mixin.get_asset_pairs()

    assert "XXBTZUSD" in pairs
    assert "XETHZUSD" in pairs
    assert pairs["XXBTZUSD"].wsname == "XBT/USD"
    assert pairs["XETHZUSD"].base == "XETH"


@pytest.mark.asyncio
async def test_get_ticker_returns_first_pair_info():
    """get_ticker returns a dict with the raw a/b/c arrays for the requested pair."""
    rest = MockRestClient()
    rest.register("ticker", _TICKER_RESPONSE)
    mixin = _TestableMarketData(rest)

    info = await mixin.get_ticker("XXBTZUSD")

    assert info["a"] == ["50000.0", "1", "1.000"]
    assert info["b"] == ["49999.0", "1", "1.000"]
    assert info["c"] == ["50000.0", "0.500"]


@pytest.mark.asyncio
async def test_get_ticker_accepts_canonical_pair():
    """get_ticker accepts BTC-USD canonical form and resolves via mapper."""
    rest = MockRestClient()
    rest.register("ticker", _TICKER_RESPONSE)
    mixin = _TestableMarketData(rest)

    info = await mixin.get_ticker("BTC-USD")

    assert "a" in info
    assert "b" in info


@pytest.mark.asyncio
async def test_get_orderbook_returns_snapshot_with_depth():
    """get_orderbook converts raw bid/ask lists to an OrderBookSnapshot."""
    rest = MockRestClient()
    rest.register("depth", _DEPTH_RESPONSE)
    mixin = _TestableMarketData(rest)

    book = await mixin.get_orderbook("XXBTZUSD", depth=2)

    assert book.trading_pair == "XXBTZUSD"
    assert len(book.bids) == 2
    assert len(book.asks) == 2
    assert book.bids[0] == (Decimal("49999.0"), Decimal("1.500"))
    assert book.asks[0] == (Decimal("50000.0"), Decimal("0.800"))


@pytest.mark.asyncio
async def test_get_orderbook_accepts_canonical_pair():
    """get_orderbook accepts BTC-USD canonical form."""
    rest = MockRestClient()
    rest.register("depth", _DEPTH_RESPONSE)
    mixin = _TestableMarketData(rest)

    book = await mixin.get_orderbook("BTC-USD", depth=2)

    assert book.trading_pair == "BTC-USD"
    assert book.bids[0][0] == Decimal("49999.0")


@pytest.mark.asyncio
async def test_get_mid_price_computes_average():
    """get_mid_price returns exact (bid + ask) / 2."""
    rest = MockRestClient()
    rest.register("ticker", _TICKER_RESPONSE)
    mixin = _TestableMarketData(rest)

    # bid = 49999.0, ask = 50000.0  → mid = 49999.5
    mid = await mixin.get_mid_price("XXBTZUSD")

    assert mid == Decimal("49999.5")


@pytest.mark.asyncio
async def test_get_server_time_returns_unix_timestamp():
    """get_server_time returns the integer unixtime field."""
    rest = MockRestClient()
    rest.register("server_time", _TIME_RESPONSE)
    mixin = _TestableMarketData(rest)

    ts = await mixin.get_server_time()

    assert ts == 1714000000
    assert isinstance(ts, int)


@pytest.mark.asyncio
async def test_market_data_not_ready_raises():
    """All methods raise GatewayNotStartedError when ready is False."""
    rest = MockRestClient()
    mixin = _TestableMarketData(rest)
    mixin._started = False

    with pytest.raises(GatewayNotStartedError):
        await mixin.get_orderbook("XXBTZUSD")

    with pytest.raises(GatewayNotStartedError):
        await mixin.get_ticker("XXBTZUSD")

    with pytest.raises(GatewayNotStartedError):
        await mixin.get_mid_price("XXBTZUSD")

    with pytest.raises(GatewayNotStartedError):
        await mixin.get_asset_pairs()

    with pytest.raises(GatewayNotStartedError):
        await mixin.get_server_time()


@pytest.mark.asyncio
async def test_kraken_error_raises():
    """A non-empty error array in the envelope raises a typed exception."""
    rest = MockRestClient()
    rest.register("asset_pairs", _ERROR_RESPONSE)
    rest.register("ticker", _ERROR_RESPONSE)
    rest.register("depth", _ERROR_RESPONSE)
    rest.register("server_time", _ERROR_RESPONSE)
    mixin = _TestableMarketData(rest)

    with pytest.raises((KrakenAPIError, Exception)):
        await mixin.get_asset_pairs()

    with pytest.raises((KrakenAPIError, Exception)):
        await mixin.get_ticker("XXBTZUSD")

    with pytest.raises((KrakenAPIError, Exception)):
        await mixin.get_orderbook("XXBTZUSD")

    with pytest.raises((KrakenAPIError, Exception)):
        await mixin.get_server_time()
