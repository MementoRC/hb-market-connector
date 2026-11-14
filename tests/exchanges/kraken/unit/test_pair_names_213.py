"""Acceptance tests for #213: Kraken gateway must send altname pair names.

hb-style dashed pairs (``BTC-USD``) must be converted through a pair-level map
built from ``AssetPairs`` (wire pair = ``altname``), never by concatenating
per-asset aliases (which yields ``XBTZUSD``, a name Kraken rejects).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from market_connector.exchanges.kraken.mixins.market_data import MarketDataMixin
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


def _pair(key: str, altname: str, wsname: str, base: str, quote: str) -> dict[str, Any]:
    return {
        "altname": altname,
        "wsname": wsname,
        "aclass_base": "currency",
        "base": base,
        "aclass_quote": "currency",
        "quote": quote,
        "lot": "unit",
        "pair_decimals": 1,
        "lot_decimals": 8,
        "lot_multiplier": 1,
        "leverage_buy": [],
        "leverage_sell": [],
        "fees": [[0, 0.26]],
        "fees_maker": [[0, 0.16]],
        "fee_volume_currency": quote,
        "margin_call": 80,
        "margin_stop": 40,
        "ordermin": "0.0001",
    }


_ASSET_PAIRS = {
    "error": [],
    "result": {
        "XXBTZUSD": _pair("XXBTZUSD", "XBTUSD", "XBT/USD", "XXBT", "ZUSD"),
        "XETHZUSD": _pair("XETHZUSD", "ETHUSD", "ETH/USD", "XETH", "ZUSD"),
        "XETHXXBT": _pair("XETHXXBT", "ETHXBT", "ETH/XBT", "XETH", "XXBT"),
        "SOLUSD": _pair("SOLUSD", "SOLUSD", "SOL/USD", "SOL", "ZUSD"),
        "XXDGZUSD": _pair("XXDGZUSD", "XDGUSD", "XDG/USD", "XXDG", "ZUSD"),
    },
}

_TICKER = {
    "error": [],
    "result": {
        "ANY": {
            "a": ["2.0", "1", "1.000"],
            "b": ["1.0", "1", "1.000"],
            "c": ["1.5", "0.5"],
            "v": ["1", "2"],
            "p": ["1", "1"],
            "t": [1, 2],
            "l": ["1", "1"],
            "h": ["2", "2"],
            "o": "1",
        }
    },
}

_DEPTH = {
    "error": [],
    "result": {
        "ANY": {
            "bids": [["1.0", "1.0", 1714000000]],
            "asks": [["2.0", "1.0", 1714000000]],
        }
    },
}

_ADD_ORDER = {
    "error": [],
    "result": {"descr": {"order": "x"}, "txid": ["OABC-DEFGH-IJKLMN"]},
}


class _RecordingRest(MockRestClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []

    async def request(self, endpoint_name, params=None, data=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append((endpoint_name, params, data))
        return await super().request(endpoint_name, params=params, data=data, headers=headers)

    def count(self, endpoint: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == endpoint)

    def last(self, endpoint: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        for name, params, data in reversed(self.calls):
            if name == endpoint:
                return params, data
        raise AssertionError(f"no {endpoint} call recorded")


class _Gateway(MarketDataMixin, OrdersMixin):
    def __init__(self, rest: _RecordingRest) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.fixture
def rest() -> _RecordingRest:
    r = _RecordingRest()
    r.register("asset_pairs", _ASSET_PAIRS)
    r.register("ticker", _TICKER)
    r.register("depth", _DEPTH)
    r.register("add_order", _ADD_ORDER)
    r.register("balance", {"error": [], "result": {"XXBT": "1.5", "ZUSD": "100.0"}})
    return r


@pytest.fixture
def gw(rest: _RecordingRest) -> _Gateway:
    return _Gateway(rest)


_CASES = [
    ("BTC-USD", "XBTUSD"),
    ("ETH-BTC", "ETHXBT"),
    ("SOL-USD", "SOLUSD"),
    ("ETH-USD", "ETHUSD"),
    ("DOGE-USD", "XDGUSD"),
]


# (a) dashed pairs reach the wire as altnames ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("hb_pair", "wire"), _CASES)
async def test_get_ticker_sends_altname(gw: _Gateway, rest: _RecordingRest, hb_pair, wire) -> None:
    await gw.get_ticker(hb_pair)
    params, _ = rest.last("ticker")
    assert params is not None and params["pair"] == wire


@pytest.mark.asyncio
@pytest.mark.parametrize(("hb_pair", "wire"), _CASES)
async def test_get_orderbook_sends_altname(
    gw: _Gateway, rest: _RecordingRest, hb_pair, wire
) -> None:
    await gw.get_orderbook(hb_pair)
    params, _ = rest.last("depth")
    assert params is not None and params["pair"] == wire


@pytest.mark.asyncio
@pytest.mark.parametrize(("hb_pair", "wire"), _CASES)
async def test_get_mid_price_sends_altname(
    gw: _Gateway, rest: _RecordingRest, hb_pair, wire
) -> None:
    await gw.get_mid_price(hb_pair)
    params, _ = rest.last("ticker")
    assert params is not None and params["pair"] == wire


# (b) pair map built from AssetPairs once, then cached ------------------------


@pytest.mark.asyncio
async def test_asset_pairs_fetched_exactly_once(gw: _Gateway, rest: _RecordingRest) -> None:
    assert rest.count("asset_pairs") == 0
    await gw.get_ticker("BTC-USD")
    await gw.get_orderbook("ETH-BTC")
    await gw.get_mid_price("SOL-USD")
    await gw.get_ticker("DOGE-USD")
    assert rest.count("asset_pairs") == 1


# (c) exchange-native input passes through unchanged --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["XBTUSD", "XXBTZUSD"])
async def test_native_pair_passes_through(gw: _Gateway, rest: _RecordingRest, native) -> None:
    await gw.get_ticker(native)
    params, _ = rest.last("ticker")
    assert params is not None and params["pair"] == native


# (d) unknown dashed pair raises before any market-data call ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_ticker", "get_orderbook", "get_mid_price"])
async def test_unknown_dashed_pair_raises_before_request(
    gw: _Gateway, rest: _RecordingRest, method
) -> None:
    with pytest.raises(ValueError, match="FOO-BAR"):
        await getattr(gw, method)("FOO-BAR")
    assert rest.count("ticker") == 0
    assert rest.count("depth") == 0


# (e) order path: place_order converts a dashed pair like market data ---------
# Decision: the issue says the gateway must never send a concatenated guess;
# place_order therefore converts dashed hb pairs to the altname (native pairs
# still pass through). cancel_order does not send the pair, so nothing to convert.


@pytest.mark.asyncio
@pytest.mark.parametrize(("hb_pair", "wire"), _CASES)
async def test_place_order_sends_altname(gw: _Gateway, rest: _RecordingRest, hb_pair, wire) -> None:
    await gw.place_order(hb_pair, OrderType.MARKET, TradeType.BUY, Decimal("1"))
    _, body = rest.last("add_order")
    assert body is not None and body["pair"] == wire


@pytest.mark.asyncio
async def test_place_order_unknown_dashed_pair_raises_before_request(
    gw: _Gateway, rest: _RecordingRest
) -> None:
    with pytest.raises(ValueError, match="FOO-BAR"):
        await gw.place_order("FOO-BAR", OrderType.MARKET, TradeType.BUY, Decimal("1"))
    assert rest.count("add_order") == 0


# (f) balances keep per-asset aliasing (regression guard) ---------------------


@pytest.mark.asyncio
async def test_balance_assets_keep_per_asset_aliases() -> None:
    assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("XXBT") == "BTC"
    assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("ZUSD") == "USD"
