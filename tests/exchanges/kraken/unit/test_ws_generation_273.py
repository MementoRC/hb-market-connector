"""Connection generation on the Kraken gateway and the core supervisor race (issue #273), RED.

* T3-gateway: ``KrakenGateway.ws_generation`` is the private connector's ``connection_generation``;
  ``wait_ws_disconnect(generation=None)`` forwards to the private connector.
* T4: the own-trades supervisor captures ``gateway.ws_generation`` BEFORE entering
  ``subscribe_own_trades`` and waits with it, so a drop+reconnect between the subscribe ack and
  the wait is still noticed (user stream goes down, then it resubscribes).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.kraken.hb_compat.kraken_connector_core import KrakenConnectorCore
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.hb_compat.event_bus import EventBus
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_USER_STREAM = "user_stream_initialized"


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


# ---------------------------------------------------------------------------
# T3-gateway
# ---------------------------------------------------------------------------


class _StubPrivate:
    def __init__(self, generation: int) -> None:
        self.connection_generation = generation
        self.wait_args: list[Any] = []

    async def wait_disconnect(self, generation: int | None = None) -> None:
        self.wait_args.append(generation)


class TestGatewayGeneration:
    def test_ws_generation_reflects_private_connector(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        stub = _StubPrivate(3)
        gw._ws_private = stub  # type: ignore[assignment]
        assert gw.ws_generation == 3
        stub.connection_generation = 4
        assert gw.ws_generation == 4

    async def test_wait_ws_disconnect_forwards_generation(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        stub = _StubPrivate(5)
        gw._ws_private = stub  # type: ignore[assignment]
        await gw.wait_ws_disconnect(2)
        await gw.wait_ws_disconnect()
        assert stub.wait_args == [2, None]


# ---------------------------------------------------------------------------
# T4. Core supervisor race
# ---------------------------------------------------------------------------


class _GenSubscription:
    def __init__(self, ws: _GenWs) -> None:
        self._ws = ws

    async def __aenter__(self) -> None:
        self._ws.enters += 1
        if self._ws.race_on_enter == self._ws.enters:
            self._ws.reconnect()  # drop + reconnect right after the ack, before the wait

    async def __aexit__(self, *_: Any) -> None:
        self._ws.exits += 1


class _GenWs:
    """Generation-aware stand-in for the private socket seams on the gateway."""

    def __init__(self) -> None:
        self.generation = 0
        self.enters = 0
        self.exits = 0
        self.race_on_enter: int | None = None
        self.wait_args: list[int | None] = []
        self.gen_at_subscribe: list[int] = []
        self._drop = asyncio.Event()

    async def subscribe_own_trades(self, _callback: Callable[[Any], None]) -> _GenSubscription:
        self.gen_at_subscribe.append(self.generation)
        return _GenSubscription(self)

    async def wait_ws_disconnect(self, generation: int | None = None) -> None:
        self.wait_args.append(generation)
        if generation is not None and generation < self.generation:
            return
        await self._drop.wait()
        self._drop = asyncio.Event()

    def reconnect(self) -> None:
        self.generation += 1

    def drop(self) -> None:
        self._drop.set()
        self.reconnect()


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[KrakenConnectorCore, _GenWs]]:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = base.FakeKrakenRest()  # type: ignore[assignment]
    gw._started = True
    fake = _GenWs()
    monkeypatch.setattr(gw, "subscribe_own_trades", fake.subscribe_own_trades)
    monkeypatch.setattr(gw, "wait_ws_disconnect", fake.wait_ws_disconnect, raising=False)
    monkeypatch.setattr(
        type(gw), "ws_generation", property(lambda _self: fake.generation), raising=False
    )
    core = KrakenConnectorCore(
        gw,
        event_bus=EventBus(name="core-273"),
        status_poll_interval=10.0,
        trading_rules_refresh_interval=10.0,
        balance_refresh_interval=10.0,
        price_refresh_interval=10.0,
        ws_reconnect_delay=0.1,
        ws_reconnect_delay_max=0.1,
        stop_grace=1.0,
    )
    yield core, fake
    with contextlib.suppress(Exception):
        await asyncio.wait_for(core.stop(), 5.0)


class TestSupervisorGenerationRace:
    async def test_drop_reconnect_before_wait_is_noticed(
        self, rig: tuple[KrakenConnectorCore, _GenWs]
    ) -> None:
        core, fake = rig
        fake.race_on_enter = 1
        history: list[bool] = []

        async def _watch() -> None:
            while True:
                history.append(bool(core.status_dict[_USER_STREAM]))
                await asyncio.sleep(0.002)

        watcher = asyncio.create_task(_watch())
        try:
            await core.start()
            await _until(lambda: fake.enters >= 2)
        finally:
            watcher.cancel()
        assert True in history
        assert False in history[history.index(True) :]
        # the generation was captured before subscribing, i.e. the pre-race one
        assert fake.wait_args[0] == fake.gen_at_subscribe[0]
        assert fake.wait_args[0] is not None

    async def test_supervisor_waits_with_generation_captured_before_subscribe(
        self, rig: tuple[KrakenConnectorCore, _GenWs]
    ) -> None:
        core, fake = rig
        fake.generation = 7
        await core.start()
        await _until(lambda: bool(fake.wait_args))
        assert fake.wait_args == [7]
        assert core.status_dict[_USER_STREAM] is True

    async def test_plain_drop_still_resubscribes(
        self, rig: tuple[KrakenConnectorCore, _GenWs]
    ) -> None:
        core, fake = rig
        await core.start()
        await _until(lambda: core.status_dict[_USER_STREAM] is True)
        fake.drop()
        await _until(lambda: fake.enters >= 2)
        await _until(lambda: core.status_dict[_USER_STREAM] is True)
