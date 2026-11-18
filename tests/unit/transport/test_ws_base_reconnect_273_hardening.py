"""Review-hardening for WsConnectorBase lifecycle (issue #273): R1-R4, R6.

R1 concurrent connect() opens one socket; R2 disconnect() during the first connect() leaves
nothing behind; R3 is_connected only after on_connected; R4 a dropped socket is closed;
R6 aux tasks are awaited on disconnect and an outer cancel of disconnect() propagates.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from market_connector.exceptions import GatewayNotStartedError
from tests.unit.transport import test_ws_base as wsb
from tests.unit.transport.test_ws_base_reconnect_273 import (
    _alive,
    _build,
    _own_tasks,
    _SocketFactory,
    _until,
    factory,  # noqa: F401  (pytest fixture)
    make,  # noqa: F401  (pytest fixture)
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from market_connector.transport.ws_base import WsConnectorBase

_NONE_ALIVE = {"_listen_loop": 0, "_heartbeat_loop": 0, "_refresh_loop": 0}


class TestConcurrentConnect:  # R1
    async def test_two_concurrent_connects_open_one_socket(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(refresh_interval=30.0)
        factory.hold = asyncio.Event()
        both = asyncio.gather(connector.connect(), connector.connect())
        await asyncio.sleep(0.03)
        factory.hold.set()
        await both
        await asyncio.sleep(0.05)
        assert len(factory.attempts) == 1
        assert len(factory.sockets) == 1
        assert _alive(connector) == {
            "_listen_loop": 1,
            "_heartbeat_loop": 1,
            "_refresh_loop": 1,
        }
        assert len(_own_tasks(connector, "_supervise")) == 1


class TestDisconnectDuringConnect:  # R2
    async def test_nothing_left_behind(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(refresh_interval=30.0)
        factory.hold = asyncio.Event()
        connecting = asyncio.create_task(connector.connect())
        await asyncio.sleep(0.03)
        disconnecting = asyncio.create_task(connector.disconnect())
        await asyncio.sleep(0.03)
        factory.hold.set()
        await asyncio.gather(connecting, disconnecting)
        await asyncio.sleep(0.05)
        assert connector.is_connected is False
        assert len(factory.sockets) == 1
        assert factory.sockets[0].closed is True
        assert _alive(connector) == _NONE_ALIVE
        assert _own_tasks(connector, "_supervise") == []


class _GatedAuth(wsb._FakeAuth):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def on_connected(self, ws_send: Callable) -> None:
        self.entered.set()
        await self.release.wait()
        await super().on_connected(ws_send)


class TestConnectedAfterAuth:  # R3
    async def test_not_connected_during_slow_on_connected(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        auth = _GatedAuth()
        connector = make(ws_auth=auth)
        connecting = asyncio.create_task(connector.connect())
        await asyncio.wait_for(auth.entered.wait(), 1.0)
        assert connector.is_connected is False
        with pytest.raises(GatewayNotStartedError):
            await connector.send({"op": "x"})
        auth.release.set()
        await connecting
        assert connector.is_connected is True


class TestDroppedSocketClosed:  # R4
    async def test_old_socket_closed_after_drop(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        old = factory.sockets[-1]
        old.drop()
        await _until(lambda: len(factory.sockets) == 2)
        assert old.closed is True


class TestAuxTasksAwaited:  # R6
    async def test_no_connector_task_pending_right_after_disconnect(
        self, factory: _SocketFactory
    ) -> None:
        before = set(asyncio.all_tasks())
        connector = _build(refresh_interval=30.0)
        await connector.connect()
        await connector.disconnect()  # no sleep: tasks must already be finished
        leftovers = [t for t in asyncio.all_tasks() - before if not t.done()]
        assert leftovers == []

    async def test_no_connector_task_pending_right_after_reconnect_replacement(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(refresh_interval=30.0)
        await connector.connect()
        old = (connector._heartbeat_task, connector._refresh_task)
        factory.sockets[-1].drop()
        await _until(lambda: len(factory.sockets) == 2)
        assert all(t is not None and t.done() for t in old)

    async def test_outer_cancel_of_disconnect_propagates(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        closing = asyncio.Event()
        gate = asyncio.Event()

        async def _slow_close() -> None:
            closing.set()
            await gate.wait()

        factory.sockets[-1].close = _slow_close  # type: ignore[method-assign]
        task = asyncio.create_task(connector.disconnect())
        await asyncio.wait_for(closing.wait(), 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
