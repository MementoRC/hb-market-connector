"""KrakenGateway start()/stop() serialization (issue #273 review, R7)."""

from __future__ import annotations

import asyncio

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway


class _StubRest:
    async def request(self, *_: object, **__: object) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        await asyncio.sleep(0.01)


class _StubWs:
    """Connector stand-in: connect() is a no-op while running, disconnect() takes a while."""

    def __init__(self) -> None:
        self.supervisors = 0
        self.management_messages: asyncio.Queue[object] = asyncio.Queue()

    async def connect(self) -> None:
        await asyncio.sleep(0)
        if self.supervisors == 0:
            self.supervisors = 1

    async def disconnect(self) -> None:
        await asyncio.sleep(0.02)
        self.supervisors = 0


class TestStartStopSerialized:
    async def test_start_during_stop_ends_started_and_consistent(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        public, private = _StubWs(), _StubWs()
        gw._rest = _StubRest()  # type: ignore[assignment]
        gw._ws = public  # type: ignore[assignment]
        gw._ws_private = private  # type: ignore[assignment]
        await gw.start()
        try:
            await asyncio.gather(gw.stop(), gw.start())
            assert gw.ready is True
            assert public.supervisors == 1
            assert private.supervisors == 1
            assert gw._ack_task is not None
            assert gw._public_drain_task is not None
            assert not gw._ack_task.done()
            assert not gw._public_drain_task.done()
        finally:
            await gw.stop()
