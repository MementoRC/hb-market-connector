"""Tests for SubscriptionsMixin.subscribe_orderbook (Stage 3)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from market_connector.exchanges.interactive_brokers.mixins.subscriptions import (
    SubscriptionsMixin,
)

# ── Doubles ────────────────────────────────────────────────────────────────────


class FakeEvent:
    """Minimal event supporting += / -= / ()."""

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, h):
        self._handlers.append(h)
        return self

    def __isub__(self, h):
        if h in self._handlers:
            self._handlers.remove(h)
        return self

    def __call__(self, *args, **kwargs):
        for h in list(self._handlers):
            h(*args, **kwargs)

    def handler_count(self) -> int:
        return len(self._handlers)


class FakeTicker:
    def __init__(self) -> None:
        self.domBids: list = []
        self.domAsks: list = []
        self.updateEvent = FakeEvent()
        self.tickByTickAllLastEvent = FakeEvent()


class FakeHandle:
    """Returned by FakeTransport.subscribe(); records close() calls."""

    def __init__(self, ticker: FakeTicker) -> None:
        self._ticker = ticker
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self) -> None:
        self._ticker = FakeTicker()
        self._handle = FakeHandle(self._ticker)
        self._subscriptions: list = []

    def subscribe(self, channel: str, contract, callback) -> FakeHandle:
        # Register the callback on appropriate event based on channel.
        if channel == "depth":
            self._ticker.updateEvent += callback
        elif channel == "trades":
            self._ticker.tickByTickAllLastEvent += callback
        self._subscriptions.append((channel, contract, callback))
        return self._handle


class FakeResolver:
    async def resolve_from_pair(self, pair: str):
        contract = MagicMock()
        contract.conId = 265598
        return contract


class ConcreteSubscriptionsHost(SubscriptionsMixin):
    def __init__(self) -> None:
        self._transport = FakeTransport()
        self._contract_resolver = FakeResolver()
        self._ready = True
        self._tick_counter: dict = {}

    async def ensure_ready(self) -> None:
        pass


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestSubscribeOrderbook:
    @pytest.mark.asyncio
    async def test_handler_registered_and_fires_on_enter(self) -> None:
        host = ConcreteSubscriptionsHost()
        received = []

        def on_update(update) -> None:
            received.append(update)

        async with host.subscribe_orderbook("AAPL-USD", on_update):
            # Simulate IB firing the depth update
            ticker = host._transport._ticker
            ticker.updateEvent(ticker)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_handle_closed_on_normal_exit(self) -> None:
        host = ConcreteSubscriptionsHost()

        async with host.subscribe_orderbook("AAPL-USD", lambda u: None):
            pass

        assert host._transport._handle.closed

    @pytest.mark.asyncio
    async def test_handle_closed_on_inner_exception(self) -> None:
        host = ConcreteSubscriptionsHost()

        with pytest.raises(RuntimeError, match="inner error"):
            async with host.subscribe_orderbook("AAPL-USD", lambda u: None):
                raise RuntimeError("inner error")

        assert host._transport._handle.closed

    @pytest.mark.asyncio
    async def test_handle_closed_on_cancelled_error(self) -> None:
        host = ConcreteSubscriptionsHost()

        with pytest.raises(asyncio.CancelledError):
            async with host.subscribe_orderbook("AAPL-USD", lambda u: None):
                raise asyncio.CancelledError

        assert host._transport._handle.closed

    @pytest.mark.asyncio
    async def test_warning_logged_when_active_subscriptions_exceed_80(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Reset the module-level counter before the test
        import sys

        subscriptions_module = sys.modules[
            "market_connector.exchanges.interactive_brokers.mixins.subscriptions"
        ]
        subscriptions_module._active_subscriptions = 0

        hosts = [ConcreteSubscriptionsHost() for _ in range(82)]
        cms = [
            host.subscribe_orderbook(f"PAIR{i}-USD", lambda u: None) for i, host in enumerate(hosts)
        ]

        logger_name = "market_connector.exchanges.interactive_brokers.mixins.subscriptions"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            entered = []
            for cm in cms:
                ctx = await cm.__aenter__()
                entered.append((cm, ctx))

        # Clean up
        for cm, _ in entered:
            await cm.__aexit__(None, None, None)

        # Reset for next test
        subscriptions_module._active_subscriptions = 0

        assert any("80" in r.message or "subscription" in r.message.lower() for r in caplog.records)


class TestSubscribeTrades:
    @pytest.fixture(autouse=True)
    def reset_state(self) -> None:
        """Reset module state before each test."""
        import sys

        subscriptions_module = sys.modules[
            "market_connector.exchanges.interactive_brokers.mixins.subscriptions"
        ]
        subscriptions_module._active_subscriptions = 0
        subscriptions_module._TRADE_ID_COUNTERS.clear()

    @pytest.mark.asyncio
    async def test_subscribe_trades_fires_callback_with_trade_event(self) -> None:
        """subscribe_trades calls callback with a TradeEvent."""
        from datetime import datetime

        host = ConcreteSubscriptionsHost()
        received = []

        def on_trade(event) -> None:
            received.append(event)

        async with host.subscribe_trades("AAPL-USD", on_trade):
            # Simulate IB firing the tick event
            ticker = host._transport._ticker
            tick = MagicMock()
            tick.time = datetime(2024, 1, 1, 9, 30, 0)
            tick.price = 150.5
            tick.size = 10
            ticker.tickByTickAllLastEvent(tick)

        assert len(received) == 1
        event = received[0]
        # Verify TradeEvent structure
        assert event.trading_pair == "AAPL-USD"
        assert event.price == 150.5
        assert event.amount == 10
        # Synthetic ID format: f"{conId}:{tick_ns}:{counter}"
        assert "265598:" in event.exchange_trade_id

    @pytest.mark.asyncio
    async def test_synthetic_id_counter_increments_within_same_second(self) -> None:
        """Synthetic ID counter increments within the same nanosecond epoch."""
        from datetime import datetime

        host = ConcreteSubscriptionsHost()
        received = []

        def on_trade(event) -> None:
            received.append(event)

        async with host.subscribe_trades("AAPL-USD", on_trade):
            ticker = host._transport._ticker
            # Emit two ticks with the same second
            for i in range(2):
                tick = MagicMock()
                tick.time = datetime(2024, 1, 1, 9, 30, 0)
                tick.price = 150.5 + i
                tick.size = 10 + i
                ticker.tickByTickAllLastEvent(tick)

        assert len(received) == 2
        # Extract counter suffixes from synthetic IDs
        id1_counter = int(received[0].exchange_trade_id.split(":")[-1])
        id2_counter = int(received[1].exchange_trade_id.split(":")[-1])
        assert id1_counter == 0
        assert id2_counter == 1

    @pytest.mark.asyncio
    async def test_synthetic_id_counter_resets_across_seconds(self) -> None:
        """Synthetic ID counter resets when moving to a new second."""
        from datetime import datetime

        host = ConcreteSubscriptionsHost()
        received = []

        def on_trade(event) -> None:
            received.append(event)

        async with host.subscribe_trades("AAPL-USD", on_trade):
            ticker = host._transport._ticker
            # Emit tick at second 1
            tick1 = MagicMock()
            tick1.time = datetime(2024, 1, 1, 9, 30, 0)
            tick1.price = 150.5
            tick1.size = 10
            ticker.tickByTickAllLastEvent(tick1)

            # Emit tick at second 2
            tick2 = MagicMock()
            tick2.time = datetime(2024, 1, 1, 9, 30, 1)
            tick2.price = 150.6
            tick2.size = 11
            ticker.tickByTickAllLastEvent(tick2)

        assert len(received) == 2
        # Both should have counter = 0 (reset across seconds)
        id1_counter = int(received[0].exchange_trade_id.split(":")[-1])
        id2_counter = int(received[1].exchange_trade_id.split(":")[-1])
        assert id1_counter == 0
        assert id2_counter == 0
