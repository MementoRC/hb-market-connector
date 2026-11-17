"""WsConnectorBase drop signalling (review hardening of #266): disconnect and clean close."""

from __future__ import annotations

import asyncio
import contextlib
import unittest.mock as mock
from unittest.mock import AsyncMock

from market_connector.transport.ws_base import WsConnectorBase
from tests.unit.transport import test_ws_base as wsb

_TARGET = "market_connector.transport.ws_base.websockets.connect"


class _EndingWs(wsb._FakeWs):
    """A socket the server closes cleanly: the frame iterator just ends."""

    def __init__(self) -> None:
        super().__init__()
        self.iterations = 0

    def __aiter__(self) -> _EndingWs:
        self.iterations += 1
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)  # yield, so a busy spin cannot starve the loop
        raise StopAsyncIteration


class TestDisconnectWakesWaiters:
    async def test_disconnect_sets_drop_event(self) -> None:
        connector = wsb._make_connector()
        await wsb._connect_with_fake_ws(connector, wsb._FakeWs())
        waiter = asyncio.create_task(connector.wait_disconnect())
        await asyncio.sleep(0)
        await connector.disconnect()
        await asyncio.wait_for(waiter, 1.0)


class TestCleanServerClose:
    async def test_clean_close_is_a_drop_and_reconnects(self) -> None:
        connector = WsConnectorBase(
            url="wss://base.example.com",
            ws_auth=wsb._FakeAuth(),
            decoder=wsb._FakeDecoder(),
            reconnect_delay=0.01,
        )
        first, second = _EndingWs(), wsb._FakeWs()
        connect = AsyncMock(side_effect=[first, second])
        with mock.patch(_TARGET, connect):
            await connector.connect()
            await asyncio.sleep(0.2)
            assert connect.await_count == 2
            assert first.iterations == 1
            await connector.disconnect()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0)

    async def test_clean_close_sets_drop_event(self) -> None:
        connector = WsConnectorBase(
            url="wss://base.example.com",
            ws_auth=wsb._FakeAuth(),
            decoder=wsb._FakeDecoder(),
            reconnect_delay=5.0,
        )
        with mock.patch(_TARGET, AsyncMock(return_value=_EndingWs())):
            await connector.connect()
            await asyncio.wait_for(connector.wait_disconnect(), 1.0)
            await connector.disconnect()
