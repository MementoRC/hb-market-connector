# tests/unit/test_hb_compat.py
"""Tests for the hb_compat bridge: sync wrapper around async gateway."""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from market_connector.hb_compat.bridge import LiveMarketAccess
from market_connector.primitives import OrderBookSnapshot


@pytest.fixture
def mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.place_order = AsyncMock(return_value="order-1")
    gw.cancel_order = AsyncMock(return_value=True)
    gw.get_mid_price = AsyncMock(return_value=Decimal("50000"))
    gw.get_balance = AsyncMock(return_value=Decimal("10000"))
    gw.get_orderbook = AsyncMock(
        return_value=OrderBookSnapshot(
            trading_pair="BTC-USDT",
            bids=[],
            asks=[],
            timestamp=0.0,
        )
    )
    return gw


@pytest.fixture
def event_loop_in_thread():
    """Run an event loop in a background thread (simulates hummingbot runtime)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


class TestLiveMarketAccess:
    def test_place_order(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        result = bridge.place_order("LIMIT", "BUY", Decimal("1"), Decimal("50000"))
        assert result == "order-1"

    def test_cancel_order(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        result = bridge.cancel_order("order-1")
        assert result is True

    def test_get_mid_price(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        price = bridge.get_mid_price()
        assert price == Decimal("50000")

    def test_timeout_raises(self, event_loop_in_thread) -> None:
        async def slow_op(*a, **kw):
            await asyncio.sleep(10)
            return "too late"

        gw = AsyncMock()
        gw.get_mid_price = slow_op
        bridge = LiveMarketAccess(
            gateway=gw,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
            timeout=0.05,
        )
        with pytest.raises(TimeoutError):
            bridge.get_mid_price()

    def test_get_available_balance(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        balance = bridge.get_available_balance("USDT")
        assert balance == Decimal("10000")
