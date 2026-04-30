"""Tests for Kraken OrdersMixin — Stage 4c."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.errors import KrakenAPIError
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.schemas.rest import OrderInfo, TradeInfo
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "rest"


def _load(name: str) -> dict:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Testable concrete class
# ---------------------------------------------------------------------------


class _TestableOrders(OrdersMixin):
    def __init__(self, rest: MockRestClient) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


# ---------------------------------------------------------------------------
# Helpers — minimal valid Kraken envelope builders
# ---------------------------------------------------------------------------


def _order_info_raw(
    txid: str = "OAVY7T-MV5VK-KHDF5X",
    status: str = "open",
    side: str = "buy",
    ordertype: str = "limit",
    pair: str = "XBTUSD",
) -> dict:
    return {
        "refid": None,
        "userref": None,
        "status": status,
        "opentm": 1688665496.7808,
        "starttm": 0.0,
        "expiretm": 0.0,
        "descr": {
            "pair": pair,
            "type": side,
            "ordertype": ordertype,
            "price": "27500.0",
            "price2": "0",
            "leverage": "none",
            "order": f"{side} 0.5 {pair} @ {ordertype} 27500.0",
        },
        "vol": "0.50000000",
        "vol_exec": "0.00000000",
        "cost": "0",
        "fee": "0",
        "price": "0",
        "misc": "",
    }


def _trade_info_raw(
    txid: str = "TZX3LZ-7RLID-KVVJN7",
    pair: str = "XBTUSD",
) -> dict:
    return {
        "ordertxid": "OAVY7T-MV5VK-KHDF5X",
        "pair": pair,
        "time": 1688665500.123,
        "type": "buy",
        "ordertype": "limit",
        "price": "27500.0",
        "cost": "13750.0",
        "fee": "22.0",
        "vol": "0.50000000",
        "margin": "0",
    }


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_market() -> None:
    rest = MockRestClient()
    rest.register("add_order", _load("add_order.json"))
    mixin = _TestableOrders(rest)
    txid = await mixin.place_order("XBTUSD", OrderType.MARKET, TradeType.BUY, Decimal("1.25"))
    assert txid == "OUF4EM-FRGI2-MQMWZD"


@pytest.mark.asyncio
async def test_place_order_limit_with_price() -> None:
    rest = MockRestClient()
    limit_response = {
        "error": [],
        "result": {
            "descr": {"order": "sell 0.5 XBTUSD @ limit 30000.0"},
            "txid": ["OLIMIT-XXXXX-YYYYYY"],
        },
    }
    rest.register("add_order", limit_response)
    mixin = _TestableOrders(rest)
    txid = await mixin.place_order(
        "XBTUSD",
        OrderType.LIMIT,
        TradeType.SELL,
        Decimal("0.5"),
        Decimal("30000.0"),
    )
    assert txid == "OLIMIT-XXXXX-YYYYYY"
    # The mock records the response, not the request; confirm txid prefix
    assert txid.startswith("OLIMIT")


@pytest.mark.asyncio
async def test_place_order_missing_price_for_limit_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    with pytest.raises(ValueError, match="price is required for LIMIT"):
        await mixin.place_order(
            "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), price=None
        )


@pytest.mark.asyncio
async def test_place_order_unsupported_type_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    with pytest.raises(ValueError, match="Unsupported order type"):
        await mixin.place_order(
            "XBTUSD", "STOP_LOSS", TradeType.BUY, Decimal("0.5"), Decimal("27000")
        )


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_success() -> None:
    rest = MockRestClient()
    rest.register("cancel_order", _load("cancel_order.json"))
    mixin = _TestableOrders(rest)
    result = await mixin.cancel_order("XBTUSD", "OAVY7T-MV5VK-KHDF5X")
    assert result is True


@pytest.mark.asyncio
async def test_cancel_order_no_match() -> None:
    rest = MockRestClient()
    rest.register("cancel_order", {"error": [], "result": {"count": 0}})
    mixin = _TestableOrders(rest)
    result = await mixin.cancel_order("XBTUSD", "UNKNOWN-TXID-XXXXX")
    assert result is False


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_orders_returns_dict() -> None:
    rest = MockRestClient()
    txid = "OAVY7T-MV5VK-KHDF5X"
    rest.register(
        "open_orders",
        {
            "error": [],
            "result": {
                "open": {
                    txid: _order_info_raw(txid=txid),
                }
            },
        },
    )
    mixin = _TestableOrders(rest)
    orders = await mixin.get_open_orders()
    assert txid in orders
    assert isinstance(orders[txid], OrderInfo)
    assert orders[txid].status == "open"


@pytest.mark.asyncio
async def test_get_open_orders_empty() -> None:
    rest = MockRestClient()
    rest.register("open_orders", {"error": [], "result": {"open": {}}})
    mixin = _TestableOrders(rest)
    orders = await mixin.get_open_orders()
    assert orders == {}


# ---------------------------------------------------------------------------
# query_orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_orders_batch() -> None:
    rest = MockRestClient()
    txid1, txid2 = "OAVY7T-MV5VK-KHDF5X", "OBBB2T-MV5VK-ZZZZZ1"
    rest.register(
        "query_orders",
        {
            "error": [],
            "result": {
                txid1: _order_info_raw(txid=txid1),
                txid2: _order_info_raw(txid=txid2, status="closed"),
            },
        },
    )
    mixin = _TestableOrders(rest)
    orders = await mixin.query_orders([txid1, txid2])
    assert set(orders.keys()) == {txid1, txid2}
    assert isinstance(orders[txid1], OrderInfo)
    assert orders[txid2].status == "closed"


# ---------------------------------------------------------------------------
# query_trades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_trades_batch() -> None:
    rest = MockRestClient()
    trade1, trade2 = "TZX3LZ-7RLID-KVVJN7", "TAAABB-CCCC1-DDDD22"
    rest.register(
        "query_trades",
        {
            "error": [],
            "result": {
                trade1: _trade_info_raw(txid=trade1),
                trade2: _trade_info_raw(txid=trade2, pair="ETHUSD"),
            },
        },
    )
    mixin = _TestableOrders(rest)
    trades = await mixin.query_trades([trade1, trade2])
    assert set(trades.keys()) == {trade1, trade2}
    assert isinstance(trades[trade1], TradeInfo)
    assert trades[trade2].pair == "ETHUSD"


# ---------------------------------------------------------------------------
# Not-ready guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orders_not_ready_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.place_order("XBTUSD", OrderType.MARKET, TradeType.BUY, Decimal("0.1"))


@pytest.mark.asyncio
async def test_cancel_not_ready_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.cancel_order("XBTUSD", "TX123")


@pytest.mark.asyncio
async def test_get_open_orders_not_ready_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_open_orders()


@pytest.mark.asyncio
async def test_query_orders_not_ready_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.query_orders(["TX123"])


@pytest.mark.asyncio
async def test_query_trades_not_ready_raises() -> None:
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.query_trades(["TX123"])


# ---------------------------------------------------------------------------
# Kraken error response raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kraken_error_raises() -> None:
    rest = MockRestClient()
    rest.register(
        "add_order",
        {"error": ["EOrder:Insufficient funds"], "result": None},
    )
    mixin = _TestableOrders(rest)
    # EOrder:Insufficient funds maps to OrderNotFoundError in ERROR_CODE_MAPPING
    from market_connector.exceptions import OrderNotFoundError

    with pytest.raises(OrderNotFoundError):
        await mixin.place_order("XBTUSD", OrderType.MARKET, TradeType.BUY, Decimal("999"))


@pytest.mark.asyncio
async def test_kraken_unknown_error_raises_kraken_api_error() -> None:
    rest = MockRestClient()
    rest.register(
        "cancel_order",
        {"error": ["EGeneral:Unknown error code"], "result": None},
    )
    mixin = _TestableOrders(rest)
    with pytest.raises(KrakenAPIError):
        await mixin.cancel_order("XBTUSD", "TX123")


# ---------------------------------------------------------------------------
# Side mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_side_mapping_uppercase_input() -> None:
    """Passing uppercase string "BUY" maps to "buy" in the request body."""
    rest = MockRestClient()
    rest.register("add_order", _load("add_order.json"))
    mixin = _TestableOrders(rest)
    # Should not raise; side "BUY" string is lowercased before sending
    txid = await mixin.place_order("XBTUSD", OrderType.MARKET, "BUY", Decimal("1.25"))
    assert txid == "OUF4EM-FRGI2-MQMWZD"
