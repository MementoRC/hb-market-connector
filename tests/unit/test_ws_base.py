# tests/unit/test_ws_base.py
"""Tests for WsConnectorBase: connect, subscribe, reconnect, message routing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exceptions import GatewayNotStartedError, SubscriptionLimitError
from market_connector.transport.ws_base import Subscription, WsConnectorBase


@pytest.fixture
def ws_client() -> WsConnectorBase:
    return WsConnectorBase(
        url="wss://ws.example.com",
        heartbeat_interval=0.05,
        reconnect_delay=0.01,
        max_reconnect_delay=0.05,
        max_subscriptions=3,
    )


class TestWsConnectorBase:
    @pytest.mark.asyncio
    async def test_not_connected_raises(self, ws_client: WsConnectorBase) -> None:
        with pytest.raises(GatewayNotStartedError):
            await ws_client.send({"type": "subscribe"})

    @pytest.mark.asyncio
    async def test_subscribe_returns_subscription(self, ws_client: WsConnectorBase) -> None:
        callback = MagicMock()
        with patch.object(ws_client, "_ws", create=True):
            ws_client._connected = True
            sub = await ws_client.subscribe("orderbook.BTC-USDT", callback)
        assert isinstance(sub, Subscription)
        assert sub.channel == "orderbook.BTC-USDT"
        assert sub.active

    @pytest.mark.asyncio
    async def test_subscription_limit_enforced(self, ws_client: WsConnectorBase) -> None:
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            for i in range(3):
                await ws_client.subscribe(f"channel.{i}", MagicMock())
            with pytest.raises(SubscriptionLimitError):
                await ws_client.subscribe("channel.overflow", MagicMock())

    @pytest.mark.asyncio
    async def test_unsubscribe_frees_slot(self, ws_client: WsConnectorBase) -> None:
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            sub = await ws_client.subscribe("channel.0", MagicMock())
            await ws_client.subscribe("channel.1", MagicMock())
            await ws_client.subscribe("channel.2", MagicMock())
            await sub.cancel()
            assert not sub.active
            # Slot freed -- should succeed
            await ws_client.subscribe("channel.3", MagicMock())

    @pytest.mark.asyncio
    async def test_message_routed_to_callback(self, ws_client: WsConnectorBase) -> None:
        callback = MagicMock()
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            await ws_client.subscribe("trades.BTC", callback)
        ws_client._route_message("trades.BTC", {"price": "50000"})
        callback.assert_called_once_with({"price": "50000"})
