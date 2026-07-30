"""Tests for SubscriptionsMixin — Phase 6, Task 6.4; updated for issue #44 (event-bus routing)."""

from __future__ import annotations

from typing import Any

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.mixins.subscriptions import SubscriptionsMixin


class _MockWs:
    """Minimal stand-in for WsConnectorBase's synchronous subscribe/unsubscribe API.

    Mirrors the (channel, pair) -> handler routing-table contract exposed by
    WsConnectorBase (now event-bus-backed, see hb_compat/event_bus.py).
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str | None], Any] = {}

    def subscribe(self, channel: str, pair: str | None, handler: Any) -> None:
        self._handlers[(channel, pair)] = handler

    def unsubscribe(self, channel: str, pair: str | None) -> None:
        self._handlers.pop((channel, pair), None)

    def deliver(self, channel: str, pair: str | None, payload: Any) -> None:
        handler = self._handlers.get((channel, pair))
        if handler is not None:
            handler(payload)


class _TestableSubs(SubscriptionsMixin):
    def __init__(self, ws: _MockWs, rest: Any = None) -> None:
        self._ws = ws
        self._rest = rest
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_subscribe_trades_invokes_callback() -> None:
    ws = _MockWs()
    received: list[Any] = []

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("BTC-USD", received.append):
        ws.deliver(
            "market_trades",
            "BTC-USD",
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
            },
        )

    assert len(received) == 1
    assert received[0].exchange_trade_id == "t1"
    # Teardown must unregister the handler from the routing table.
    assert ("market_trades", "BTC-USD") not in ws._handlers


@pytest.mark.asyncio
async def test_subscribe_trades_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_trades("BTC-USD", lambda e: None)


@pytest.mark.asyncio
async def test_subscribe_orderbook_invokes_callback() -> None:
    ws = _MockWs()
    received: list[Any] = []

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("BTC-USD", received.append):
        ws.deliver(
            "level2",
            "BTC-USD",
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
            },
        )

    assert len(received) == 1
    assert received[0].trading_pair == "BTC-USD"
    assert ("level2", "BTC-USD") not in ws._handlers


@pytest.mark.asyncio
async def test_subscribe_orderbook_filters_other_products() -> None:
    ws = _MockWs()
    received: list[Any] = []

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("BTC-USD", received.append):
        # Event for a different product — should be filtered out
        ws.deliver(
            "level2",
            "BTC-USD",
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
            },
        )

    assert len(received) == 0


@pytest.mark.asyncio
async def test_subscribe_orderbook_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_orderbook("BTC-USD", lambda u: None)


@pytest.mark.asyncio
async def test_subscribe_trades_filters_other_products() -> None:
    ws = _MockWs()
    received: list[Any] = []

    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("BTC-USD", received.append):
        ws.deliver(
            "market_trades",
            "BTC-USD",
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
            },
        )

    assert len(received) == 0


@pytest.mark.asyncio
async def test_subscribe_orderbook_registers_on_real_ws_routing_table() -> None:
    """Integration-style check: mixin wiring works against the real WsConnectorBase.

    Verifies the coinbase subscriptions mixin actually drives
    WsConnectorBase's event-bus-backed routing table end to end (issue #44),
    not just the local `_MockWs` test double above.
    """
    from market_connector.exchanges.coinbase.specs import COINBASE_WS_SHAPE_DECODER_SPEC
    from market_connector.transport.ws_base import WsConnectorBase
    from market_connector.ws_models.auth_models import PassThroughAuth
    from market_connector.ws_models.decoder import JsonEnvelopeDecoder

    ws = WsConnectorBase(
        url="wss://example.invalid",
        ws_auth=PassThroughAuth(),
        decoder=JsonEnvelopeDecoder(**COINBASE_WS_SHAPE_DECODER_SPEC),
    )
    received: list[Any] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("BTC-USD", received.append):
        assert ("level2", "BTC-USD") in ws._routing_table
        routed = ws._routing_table.route(
            "level2",
            "BTC-USD",
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
            },
        )
        assert routed

    assert len(received) == 1
    assert ("level2", "BTC-USD") not in ws._routing_table
