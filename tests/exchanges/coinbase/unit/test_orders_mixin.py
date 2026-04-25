"""Tests for OrdersMixin — Phase 6, Task 6.3."""

from decimal import Decimal

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _TestableOrders(OrdersMixin):
    def __init__(self, rest):
        self._rest = rest
        self._endpoints = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_place_limit_order():
    rest = MockRestClient()
    rest.register(
        "place_order",
        {
            "success": True,
            "order_id": "o1",
            "success_response": {
                "order_id": "o1",
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "c1",
            },
        },
    )
    mixin = _TestableOrders(rest)
    client_id = await mixin.place_order(
        "BTC-USD",
        OrderType.LIMIT,
        TradeType.BUY,
        Decimal("0.5"),
        Decimal("50000"),
    )
    # Returns a generated client_id (coinbase-<uuid>) — not echoed back from mock
    assert client_id.startswith("coinbase-")


@pytest.mark.asyncio
async def test_cancel_order_returns_true():
    rest = MockRestClient()
    rest.register("cancel_orders", {"results": [{"success": True, "order_id": "o1"}]})
    mixin = _TestableOrders(rest)
    assert await mixin.cancel_order("BTC-USD", "c1") is True


@pytest.mark.asyncio
async def test_get_open_orders_filters_by_pair():
    rest = MockRestClient()
    rest.register(
        "list_orders",
        {
            "orders": [
                {
                    "order_id": "o1",
                    "client_order_id": "c1",
                    "product_id": "BTC-USD",
                    "side": "BUY",
                    "status": "OPEN",
                    "order_configuration": {
                        "limit_limit_gtc": {"base_size": "0.5", "limit_price": "50000"}
                    },
                    "filled_size": "0",
                    "average_filled_price": "0",
                },
            ]
        },
    )
    mixin = _TestableOrders(rest)
    orders = await mixin.get_open_orders("BTC-USD")
    assert len(orders) == 1
    assert orders[0].client_order_id == "c1"


@pytest.mark.asyncio
async def test_place_order_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.place_order(
            "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.1"), Decimal("50000")
        )


@pytest.mark.asyncio
async def test_place_limit_maker_order():
    rest = MockRestClient()
    rest.register(
        "place_order",
        {
            "success": True,
            "order_id": "o2",
            "success_response": {
                "order_id": "o2",
                "product_id": "BTC-USD",
                "side": "SELL",
                "client_order_id": "c2",
            },
        },
    )
    mixin = _TestableOrders(rest)
    client_id = await mixin.place_order(
        "BTC-USD",
        OrderType.LIMIT_MAKER,
        TradeType.SELL,
        Decimal("0.5"),
        Decimal("60000"),
    )
    assert client_id.startswith("coinbase-")


@pytest.mark.asyncio
async def test_place_market_order():
    rest = MockRestClient()
    rest.register(
        "place_order",
        {
            "success": True,
            "order_id": "o3",
            "success_response": {
                "order_id": "o3",
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "c3",
            },
        },
    )
    mixin = _TestableOrders(rest)
    client_id = await mixin.place_order(
        "BTC-USD",
        OrderType.MARKET,
        TradeType.BUY,
        Decimal("0.1"),
        None,
    )
    assert client_id.startswith("coinbase-")


@pytest.mark.asyncio
async def test_place_order_failure_raises_order_rejected():
    from market_connector.exceptions import OrderRejectedError

    rest = MockRestClient()
    rest.register(
        "place_order",
        {
            "success": False,
            "failure_reason": "INSUFFICIENT_FUND",
        },
    )
    mixin = _TestableOrders(rest)
    with pytest.raises(OrderRejectedError):
        await mixin.place_order(
            "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("999"), Decimal("50000")
        )


@pytest.mark.asyncio
async def test_cancel_order_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.cancel_order("BTC-USD", "c1")


@pytest.mark.asyncio
async def test_get_open_orders_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableOrders(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_open_orders("BTC-USD")
