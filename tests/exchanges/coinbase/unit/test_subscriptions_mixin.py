"""Tests for SubscriptionsMixin — Phase 6, Task 6.4."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.mixins.subscriptions import SubscriptionsMixin


class _TestableSubs(SubscriptionsMixin):
    def __init__(self, ws, rest=None):
        self._ws = ws
        self._rest = rest
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_subscribe_trades_invokes_callback():
    ws = MagicMock()
    received: list = []
    captured_cb: dict = {}

    async def subscribe(channel, callback):
        captured_cb["cb"] = callback
        sub = MagicMock()
        sub.cancel = AsyncMock()
        return sub

    ws.subscribe = subscribe

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("BTC-USD", received.append):
        # Simulate WS message delivery
        captured_cb["cb"](
            {
                "events": [
                    {
                        "type": "update",
                        "trades": [
                            {
                                "trade_id": "t1",
                                "product_id": "BTC-USD",
                                "price": "50000",
                                "size": "0.5",
                                "side": "BUY",
                                "time": "2026-04-24T12:00:00Z",
                            }
                        ],
                    }
                ],
            }
        )

    assert len(received) == 1
    assert received[0].exchange_trade_id == "t1"


@pytest.mark.asyncio
async def test_subscribe_trades_not_ready_raises():
    ws = MagicMock()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_trades("BTC-USD", lambda e: None)


@pytest.mark.asyncio
async def test_subscribe_orderbook_invokes_callback():
    ws = MagicMock()
    received: list = []
    captured_cb: dict = {}

    async def subscribe(channel, callback):
        captured_cb["cb"] = callback
        sub = MagicMock()
        sub.cancel = AsyncMock()
        return sub

    ws.subscribe = subscribe

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("BTC-USD", received.append):
        captured_cb["cb"](
            {
                "events": [
                    {
                        "type": "update",
                        "product_id": "BTC-USD",
                        "updates": [
                            {
                                "side": "bid",
                                "event_time": "2026-04-24T12:00:00Z",
                                "price_level": "50000",
                                "new_quantity": "0.5",
                            }
                        ],
                    }
                ]
            }
        )

    assert len(received) == 1
    assert received[0].trading_pair == "BTC-USD"


@pytest.mark.asyncio
async def test_subscribe_orderbook_filters_other_products():
    ws = MagicMock()
    received: list = []
    captured_cb: dict = {}

    async def subscribe(channel, callback):
        captured_cb["cb"] = callback
        sub = MagicMock()
        sub.cancel = AsyncMock()
        return sub

    ws.subscribe = subscribe

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("BTC-USD", received.append):
        # Event for a different product — should be filtered out
        captured_cb["cb"](
            {
                "events": [
                    {
                        "type": "update",
                        "product_id": "ETH-USD",
                        "updates": [
                            {
                                "side": "bid",
                                "event_time": "2026-04-24T12:00:00Z",
                                "price_level": "3000",
                                "new_quantity": "1.0",
                            }
                        ],
                    }
                ]
            }
        )

    assert len(received) == 0


@pytest.mark.asyncio
async def test_subscribe_orderbook_not_ready_raises():
    ws = MagicMock()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_orderbook("BTC-USD", lambda u: None)


@pytest.mark.asyncio
async def test_subscribe_trades_filters_other_products():
    ws = MagicMock()
    received: list = []
    captured_cb: dict = {}

    async def subscribe(channel, callback):
        captured_cb["cb"] = callback
        sub = MagicMock()
        sub.cancel = AsyncMock()
        return sub

    ws.subscribe = subscribe

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("BTC-USD", received.append):
        captured_cb["cb"](
            {
                "events": [
                    {
                        "type": "update",
                        "trades": [
                            {
                                "trade_id": "t2",
                                "product_id": "ETH-USD",  # different product
                                "price": "3000",
                                "size": "1.0",
                                "side": "BUY",
                                "time": "2026-04-24T12:00:00Z",
                            }
                        ],
                    }
                ],
            }
        )

    assert len(received) == 0
