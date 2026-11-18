"""WsConnectorBase reconnect correctness (issue #273), RED step.

Pinned contract:

* T1 single owner: after N>=3 drops (exception and clean end), each followed by a successful
  reconnect, exactly one listen / heartbeat / refresh task of the connector is alive.
* T2 ``is_connected``: False from drop detection until the reconnect succeeds; ``send()`` raises
  ``GatewayNotStartedError`` meanwhile.
* T3 ``connection_generation`` increments on every successful connect();
  ``wait_disconnect(generation)`` returns immediately for an already-dropped generation.
* T5 explicit ``disconnect()`` wakes every waiter and stops reconnecting for good.
* T6 backoff still grows, caps at ``max_reconnect_delay`` and a failed reconnect is retried.
"""

from __future__ import annotations

import asyncio
import time
import unittest.mock as mock
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.transport.ws_base import WsConnectorBase
from tests.unit.transport import test_ws_base as wsb

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_TARGET = "market_connector.transport.ws_base.websockets.connect"
_ERR = object()
_END = object()


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


class _DroppableWs(wsb._FakeWs):
    """Fake socket that can be dropped with an exception or closed cleanly."""

    _dead: BaseException | None = None

    def drop(self) -> None:
        self._queue.put_nowait(_ERR)  # type: ignore[arg-type]

    def end(self) -> None:
        self._queue.put_nowait(_END)  # type: ignore[arg-type]

    async def __anext__(self) -> str:
        if self._dead is not None:  # a dropped socket stays dead, like a real one
            await asyncio.sleep(0)
            raise self._dead
        item = await self._queue.get()
        if item is _ERR:
            self._dead = ConnectionError("dropped")
            raise self._dead
        if item is _END:
            self._dead = StopAsyncIteration()
            raise self._dead
        return item


class _SocketFactory:
    """Stand-in for ``websockets.connect``: fresh fake per call, scriptable failures/holds."""

    def __init__(self) -> None:
        self.sockets: list[_DroppableWs] = []
        self.attempts: list[float] = []
        self.fail_next = 0
        self.hold: asyncio.Event | None = None  # while set-up and unset, connects block

    async def __call__(self, *_: Any, **__: Any) -> _DroppableWs:
        self.attempts.append(time.monotonic())
        if self.hold is not None:
            await self.hold.wait()
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("connect failed")
        ws = _DroppableWs()
        self.sockets.append(ws)
        return ws


def _own_tasks(connector: WsConnectorBase, name: str) -> list[asyncio.Task[Any]]:
    found = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        if task.done() or frame is None:
            continue
        if getattr(coro, "__name__", "") == name and frame.f_locals.get("self") is connector:
            found.append(task)
    return found


def _alive(connector: WsConnectorBase) -> dict[str, int]:
    names = ("_listen_loop", "_heartbeat_loop", "_refresh_loop")
    return {n: len(_own_tasks(connector, n)) for n in names}


def _build(**kw: Any) -> WsConnectorBase:
    opts: dict[str, Any] = {
        "url": "wss://base.example.com",
        "ws_auth": wsb._FakeAuth(),
        "decoder": wsb._FakeDecoder(),
        "reconnect_delay": 0.01,
        "max_reconnect_delay": 0.04,
        "heartbeat_interval": 30.0,
    }
    opts.update(kw)
    return WsConnectorBase(**opts)


@pytest.fixture
async def factory() -> AsyncIterator[_SocketFactory]:
    fac = _SocketFactory()
    with mock.patch(_TARGET, fac):
        yield fac


@pytest.fixture
async def make(factory: _SocketFactory) -> AsyncIterator[Callable[..., WsConnectorBase]]:
    built: list[WsConnectorBase] = []

    def _make(**kw: Any) -> WsConnectorBase:
        connector = _build(**kw)
        built.append(connector)
        return connector

    yield _make
    for connector in built:
        await connector.disconnect()
    await asyncio.sleep(0)


async def _drop_and_reconnect(factory: _SocketFactory, kind: str) -> None:
    before = len(factory.sockets)
    sock = factory.sockets[-1]
    sock.drop() if kind == "error" else sock.end()
    await _until(lambda: len(factory.sockets) == before + 1)
    await asyncio.sleep(0.05)  # let any (buggy) extra tasks show up


# ---------------------------------------------------------------------------
# T1. Single owner
# ---------------------------------------------------------------------------


class TestSingleOwner:
    @pytest.mark.parametrize("refresh", [None, 30.0])
    async def test_one_task_each_after_repeated_drops(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase], refresh: float | None
    ) -> None:
        connector = make(refresh_interval=refresh)
        await connector.connect()
        for kind in ("error", "end", "error", "end"):
            await _drop_and_reconnect(factory, kind)
        alive = _alive(connector)
        assert alive["_listen_loop"] == 1
        assert alive["_heartbeat_loop"] == 1
        assert alive["_refresh_loop"] == (1 if refresh is not None else 0)

    async def test_earlier_tasks_are_done(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(refresh_interval=30.0)
        await connector.connect()
        first = (connector._listen_task, connector._heartbeat_task, connector._refresh_task)
        for kind in ("error", "end", "error"):
            await _drop_and_reconnect(factory, kind)
        assert all(t is not None and t.done() for t in first)
        current = (connector._listen_task, connector._heartbeat_task, connector._refresh_task)
        assert all(t is not None and not t.done() for t in current)

    async def test_frames_still_routed_after_reconnects(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        for kind in ("error", "end", "error"):
            await _drop_and_reconnect(factory, kind)
        # exactly one consumer on the live socket: a queued frame is taken once, queue drains
        factory.sockets[-1].push({"x": 1})
        await _until(lambda: factory.sockets[-1]._queue.empty())


# ---------------------------------------------------------------------------
# T2. Reconnect window
# ---------------------------------------------------------------------------


class TestReconnectWindow:
    async def test_connected_flag_lifecycle(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        assert connector.is_connected is True
        factory.hold = asyncio.Event()
        factory.sockets[-1].drop()
        await _until(lambda: not connector.is_connected)
        factory.hold.set()
        await _until(lambda: connector.is_connected)
        assert len(factory.sockets) == 2

    @pytest.mark.parametrize("kind", ["error", "end"])
    async def test_send_raises_gateway_not_started_in_window(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase], kind: str
    ) -> None:
        connector = make()
        await connector.connect()
        dead = factory.sockets[-1]
        factory.hold = asyncio.Event()
        dead.drop() if kind == "error" else dead.end()
        await _until(lambda: not connector.is_connected)
        with pytest.raises(GatewayNotStartedError):
            await connector.send({"op": "x"})
        assert dead.sent == []
        factory.hold.set()
        await _until(lambda: connector.is_connected)
        await connector.send({"op": "y"})
        assert len(factory.sockets[-1].sent) == 1


# ---------------------------------------------------------------------------
# T3. Generation
# ---------------------------------------------------------------------------


class TestGeneration:
    async def test_generation_increments_per_successful_connect(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        g0 = connector.connection_generation
        await connector.connect()
        assert connector.connection_generation == g0 + 1
        await _drop_and_reconnect(factory, "error")
        assert connector.connection_generation == g0 + 2
        await _drop_and_reconnect(factory, "end")
        assert connector.connection_generation == g0 + 3

    async def test_failed_connect_does_not_bump_generation(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        gen = connector.connection_generation
        factory.fail_next = 2
        factory.sockets[-1].drop()
        await _until(lambda: len(factory.attempts) >= 2)
        assert connector.connection_generation == gen
        await _until(lambda: connector.is_connected)
        assert connector.connection_generation == gen + 1

    async def test_old_generation_returns_immediately(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        old = connector.connection_generation
        await _drop_and_reconnect(factory, "error")
        assert connector.connection_generation > old
        await asyncio.wait_for(connector.wait_disconnect(old), 0.5)

    async def test_current_generation_waits_for_its_drop(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        waiter = asyncio.create_task(connector.wait_disconnect(connector.connection_generation))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        factory.sockets[-1].drop()
        await asyncio.wait_for(waiter, 1.0)

    async def test_none_waits_for_current_connection_drop(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        await _drop_and_reconnect(factory, "end")  # an older drop must not satisfy None
        waiter = asyncio.create_task(connector.wait_disconnect())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        factory.sockets[-1].drop()
        await asyncio.wait_for(waiter, 1.0)

    async def test_generation_wait_in_reconnect_window_returns_immediately(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        gen = connector.connection_generation
        factory.hold = asyncio.Event()
        factory.sockets[-1].end()
        await _until(lambda: not connector.is_connected)
        await asyncio.wait_for(connector.wait_disconnect(gen), 0.5)
        factory.hold.set()


# ---------------------------------------------------------------------------
# T5. Explicit disconnect
# ---------------------------------------------------------------------------


class TestExplicitDisconnect:
    async def test_wakes_every_waiter(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        await _drop_and_reconnect(factory, "error")
        gen = connector.connection_generation
        waiters = [
            asyncio.create_task(connector.wait_disconnect()),
            asyncio.create_task(connector.wait_disconnect(gen)),
            asyncio.create_task(connector.wait_disconnect(gen + 5)),
        ]
        await asyncio.sleep(0.02)
        assert not any(w.done() for w in waiters)
        await connector.disconnect()
        await asyncio.wait_for(asyncio.gather(*waiters), 1.0)
        assert connector.is_connected is False

    async def test_waiter_after_disconnect_returns_immediately(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        gen = connector.connection_generation
        await connector.disconnect()
        await asyncio.wait_for(connector.wait_disconnect(gen), 0.5)
        await asyncio.wait_for(connector.wait_disconnect(), 0.5)

    async def test_no_tasks_alive_afterwards(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(refresh_interval=30.0)
        await connector.connect()
        await _drop_and_reconnect(factory, "error")
        await connector.disconnect()
        await asyncio.sleep(0.05)
        assert _alive(connector) == {"_listen_loop": 0, "_heartbeat_loop": 0, "_refresh_loop": 0}
        with pytest.raises(GatewayNotStartedError):
            await connector.send({"op": "x"})

    async def test_no_reconnect_after_disconnect_during_backoff(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(reconnect_delay=0.15, max_reconnect_delay=0.3)
        await connector.connect()
        factory.sockets[-1].drop()
        await _until(lambda: not connector.is_connected)
        await connector.disconnect()
        await asyncio.sleep(0.5)
        assert len(factory.attempts) == 1
        assert connector.is_connected is False
        assert _alive(connector)["_listen_loop"] == 0

    async def test_disconnect_during_pending_reconnect_stays_down(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make()
        await connector.connect()
        factory.hold = asyncio.Event()
        factory.sockets[-1].drop()
        await _until(lambda: len(factory.attempts) >= 2)  # reconnect in flight
        await connector.disconnect()
        factory.hold.set()
        await asyncio.sleep(0.2)
        assert connector.is_connected is False
        assert _alive(connector) == {"_listen_loop": 0, "_heartbeat_loop": 0, "_refresh_loop": 0}


# ---------------------------------------------------------------------------
# T6. Backoff regression guard
# ---------------------------------------------------------------------------


class TestBackoff:
    async def test_backoff_grows_caps_and_failed_reconnect_is_retried(
        self, factory: _SocketFactory, make: Callable[..., WsConnectorBase]
    ) -> None:
        connector = make(reconnect_delay=0.02, max_reconnect_delay=0.08)
        await connector.connect()
        factory.fail_next = 6
        factory.sockets[-1].drop()
        await _until(lambda: len(factory.sockets) == 2, timeout=5.0)  # retried until success
        gaps = [b - a for a, b in zip(factory.attempts[1:], factory.attempts[2:], strict=False)]
        assert len(gaps) >= 5
        assert gaps[0] < 0.07  # early retries use the small delays
        assert max(gaps) >= 0.07  # grew to (about) the cap
        assert max(gaps) < 0.4  # and never beyond it
        assert gaps[-1] > gaps[0] * 1.5  # relative growth
