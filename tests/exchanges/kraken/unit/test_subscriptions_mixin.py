"""Tests for Kraken SubscriptionsMixin — Stage 4d."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.mixins.subscriptions import SubscriptionsMixin

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.schemas.ws import (
        BookSnapshot,
        OpenOrdersEvent,
        OwnTradesEvent,
        TradeEvent,
    )

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "ws"


def _load(name: str) -> Any:
    with (_FIXTURES_DIR / name).open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Concrete test double
# ---------------------------------------------------------------------------


class _MockWs:
    """Minimal WS stub that captures subscribe registrations.

    - subscribe(channel, pair, handler): synchronous registration
    - send(msg): async no-op (captured via AsyncMock)
    - _handlers: mirrors WsConnectorBase routing table
    - deliver(channel, pair, raw): calls the registered handler with raw
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str | None], Any] = {}
        self.send = AsyncMock()

    def subscribe(self, channel: str, pair: str | None, handler: Any) -> None:
        self._handlers[(channel, pair)] = handler

    def deliver(self, channel: str, pair: str | None, raw: Any) -> None:
        """Invoke the registered handler for *(channel, pair)* with *raw*."""
        handler = self._handlers.get((channel, pair))
        if handler is not None:
            handler(raw)


class _TestableSubs(SubscriptionsMixin):
    def __init__(self, ws: _MockWs) -> None:
        self._ws = ws
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


# ---------------------------------------------------------------------------
# subscribe_orderbook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_orderbook_routes_book_snapshot() -> None:
    raw = _load("book_snapshot.json")
    ws = _MockWs()
    received: list[BookSnapshot] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("XBT/USD", received.append, depth=25):
        ws.deliver("book-25", "XBT/USD", raw)

    assert len(received) == 1
    snap = received[0]
    assert len(snap.asks) == 3
    assert snap.asks[0].price == "5541.30000"
    assert len(snap.bids) == 3
    assert snap.bids[0].price == "5541.20000"


@pytest.mark.asyncio
async def test_subscribe_orderbook_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_orderbook("XBT/USD", lambda u: None)


@pytest.mark.asyncio
async def test_subscribe_orderbook_sends_subscribe_message() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("XBT/USD", lambda u: None, depth=10):
        pass

    # First call is subscribe, second is unsubscribe
    subscribe_call = ws.send.call_args_list[0]
    msg = subscribe_call.args[0]
    assert msg["event"] == "subscribe"
    assert msg["pair"] == ["XBT/USD"]
    assert msg["subscription"]["name"] == "book"
    assert msg["subscription"]["depth"] == 10


@pytest.mark.asyncio
async def test_subscribe_orderbook_sends_unsubscribe_on_exit() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("XBT/USD", lambda u: None):
        pass

    assert ws.send.call_count == 2
    unsubscribe_msg = ws.send.call_args_list[1].args[0]
    assert unsubscribe_msg["event"] == "unsubscribe"


# ---------------------------------------------------------------------------
# subscribe_trades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_trades_routes_trade_event() -> None:
    raw = _load("trade_event.json")
    ws = _MockWs()
    received: list[TradeEvent] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("XBT/USD", received.append):
        ws.deliver("trade", "XBT/USD", raw)

    assert len(received) == 1
    event = received[0]
    assert event.channel == "trade"
    assert event.pair == "XBT/USD"
    assert len(event.trades) == 2
    assert event.trades[0].price == "5541.20000"
    assert event.trades[0].side == "s"


@pytest.mark.asyncio
async def test_subscribe_trades_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_trades("XBT/USD", lambda e: None)


@pytest.mark.asyncio
async def test_subscribe_trades_sends_subscribe_message() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("XBT/USD", lambda e: None):
        pass

    subscribe_msg = ws.send.call_args_list[0].args[0]
    assert subscribe_msg["event"] == "subscribe"
    assert subscribe_msg["pair"] == ["XBT/USD"]
    assert subscribe_msg["subscription"]["name"] == "trade"


# ---------------------------------------------------------------------------
# subscribe_own_trades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_own_trades_routes_private_frame() -> None:
    raw = _load("own_trades.json")
    ws = _MockWs()
    received: list[OwnTradesEvent] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_own_trades(received.append):
        ws.deliver("ownTrades", None, raw)

    assert len(received) == 1
    event = received[0]
    assert event.sequence == 2948
    assert "TDLH43-DVQXD-2KHVYY" in event.trades
    assert event.trades["TDLH43-DVQXD-2KHVYY"].type == "buy"


@pytest.mark.asyncio
async def test_subscribe_own_trades_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_own_trades(lambda e: None)


# ---------------------------------------------------------------------------
# subscribe_open_orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_open_orders_routes_private_frame() -> None:
    # Synthetic openOrders frame (same shape as ownTrades)
    raw = [
        [
            {
                "ABCD12-EFGH34-IJKL56": {
                    "status": "open",
                    "vol": "1.00000000",
                    "vol_exec": "0.00000000",
                    "cost": "0.00000",
                    "fee": "0.00000",
                    "avg_price": "0.00000",
                }
            }
        ],
        "openOrders",
        {"sequence": 5},
    ]
    ws = _MockWs()
    received: list[OpenOrdersEvent] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_open_orders(received.append):
        ws.deliver("openOrders", None, raw)

    assert len(received) == 1
    event = received[0]
    assert event.sequence == 5
    assert "ABCD12-EFGH34-IJKL56" in event.orders
    assert event.orders["ABCD12-EFGH34-IJKL56"].status == "open"


@pytest.mark.asyncio
async def test_subscribe_open_orders_not_ready_raises() -> None:
    ws = _MockWs()
    mixin = _TestableSubs(ws)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.subscribe_open_orders(lambda e: None)


# ---------------------------------------------------------------------------
# Decoder filtering — control frames must not reach callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decoder_filters_heartbeat() -> None:
    raw = _load("heartbeat.json")
    ws = _MockWs()
    received: list[Any] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("XBT/USD", received.append):
        ws.deliver("book-25", "XBT/USD", raw)

    assert len(received) == 0, "heartbeat must not trigger the callback"


@pytest.mark.asyncio
async def test_decoder_filters_system_status() -> None:
    raw = _load("system_status.json")
    ws = _MockWs()
    received: list[Any] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_trades("XBT/USD", received.append):
        ws.deliver("trade", "XBT/USD", raw)

    assert len(received) == 0, "systemStatus must not trigger the callback"


@pytest.mark.asyncio
async def test_subscribe_ack_handled_silently() -> None:
    raw = _load("subscribe_ack.json")
    ws = _MockWs()
    received: list[Any] = []
    mixin = _TestableSubs(ws)

    async with await mixin.subscribe_orderbook("XBT/USD", received.append):
        ws.deliver("book-25", "XBT/USD", raw)

    assert len(received) == 0, "subscriptionStatus ack must not trigger the callback"
