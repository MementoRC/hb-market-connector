"""End-to-end: ``KrakenConnectorCore`` (#266) over a real ``KrakenGateway`` private WS (#274), RED.

Only the sockets (``websockets.connect``) and the REST transport are faked. The private connector
uses the gateway's own auth, ack gate, STOCK Kraken decoder and ``wait_ws_disconnect``; frames are
pushed as realistic JSON text (no decoder bypass).

Pinned: an error ack keeps ``user_stream_initialized`` False and the supervisor retries; a
``subscribed`` ack flips it True; an ownTrades frame on the private socket reaches
``on_own_trades``; dropping the private socket flips it back to False (and it recovers).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from typing import TYPE_CHECKING, Any

import pytest

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit.test_private_ws_274 import (
    TokenRest,
    auto_ack,
    extra_reply,
    open_rig,
    reject,
    until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from market_connector.hb_compat.event_bus import EventBus
    from market_connector.transport.response import Response
    from tests.exchanges.kraken.unit.test_private_ws_274 import Rig

_CORE_MOD = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"
_USER_STREAM = "user_stream_initialized"
_ACK_TIMEOUT = 0.3


class CoreRest(base.FakeKrakenRest):
    """The core's REST fake plus the endpoints the gateway lifecycle needs."""

    def __init__(self) -> None:
        super().__init__()
        self.token = TokenRest().token
        self.closed = False

    async def request(self, endpoint_name: str, *args: Any, **kwargs: Any) -> Response:
        reply = extra_reply(endpoint_name, self.token)
        if reply is not None:
            return reply
        return await super().request(endpoint_name, *args, **kwargs)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def bus() -> EventBus:
    return base.EventBus(name="core-e2e-274")


@pytest.fixture
def seen(bus: EventBus) -> base.Recorder:
    return base.Recorder(bus)


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    async with open_rig(
        monkeypatch,
        CoreRest(),
        ack_timeout=_ACK_TIMEOUT,
        private_reconnect_delay=0.01,
    ) as opened:
        yield opened


@pytest.fixture
async def build(rig: Rig, bus: EventBus, seen: base.Recorder) -> AsyncIterator[Callable[..., Any]]:
    cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore
    cores: list[Any] = []

    def _build(**options: Any) -> Any:
        opts: dict[str, Any] = {
            "status_poll_interval": 10.0,
            "trading_rules_refresh_interval": 10.0,
            "balance_refresh_interval": 10.0,
            "price_refresh_interval": 10.0,
            "ws_reconnect_delay": 0.01,
            "ws_reconnect_delay_max": 0.05,
            "stop_grace": 1.0,
        }
        opts.update(options)
        core = cls(rig.gw, event_bus=bus, **opts)
        cores.append(core)
        return core

    yield _build
    for core in cores:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(core.stop(), 5.0)


def _ws_frame(trade_id: str) -> list[Any]:
    """A realistic Kraken WS v1 ownTrades frame (pushed as JSON text, stock decoder)."""
    detail = {
        "ordertxid": base._TXID,
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        "userref": 0,
        "pair": "XBT/USD",
        "time": "1700000000.5123",
        "type": "sell",
        "ordertype": "limit",
        "price": "27000.0",
        "cost": "5400.0",
        "fee": "8.64",
        "vol": "0.2",
        "margin": "0",
    }
    return [[{trade_id: detail}], "ownTrades", {"sequence": 1}]


class TestUserStreamAck:
    async def test_error_ack_keeps_stream_down_and_retries_then_subscribed_flips_it_up(
        self, rig: Rig, build: Callable[..., Any]
    ) -> None:
        rig.responder = reject
        core = build()
        await core.start()
        await until(lambda: len(rig.priv.frames("subscribe")) >= 3, timeout=5.0)
        assert core.status_dict[_USER_STREAM] is False
        assert core.ready is False
        reqids = [f["reqid"] for f in rig.priv.frames("subscribe")]
        assert len(set(reqids)) == len(reqids)  # every retry is a fresh, separately gated subscribe

        rig.responder = auto_ack
        await until(lambda: core.status_dict[_USER_STREAM] is True, timeout=5.0)
        await until(lambda: core.ready, timeout=5.0)

    async def test_unanswered_subscribe_never_marks_the_stream_up(
        self, rig: Rig, build: Callable[..., Any]
    ) -> None:
        rig.responder = lambda _frame: None
        core = build()
        await core.start()
        await until(lambda: len(rig.priv.frames("subscribe")) >= 2, timeout=5.0)
        assert core.status_dict[_USER_STREAM] is False


class TestUserStreamFlow:
    async def test_own_trades_frame_on_the_private_socket_reaches_on_own_trades(
        self, rig: Rig, build: Callable[..., Any], seen: base.Recorder
    ) -> None:
        core = build()
        await core.start()
        await until(lambda: core.ready, timeout=5.0)
        client_id = await base._place(core, seen, "sell")
        rig.priv.push(_ws_frame("T1"))
        await seen.wait(base.FILLED)
        (fill,) = seen.of(base.FILLED)
        assert fill.order_id == client_id
        assert fill.exchange_trade_id == "T1"

    async def test_dropping_the_private_socket_flips_the_stream_down(
        self, rig: Rig, build: Callable[..., Any]
    ) -> None:
        core = build(ws_reconnect_delay=0.3, ws_reconnect_delay_max=1.0)
        await core.start()
        await until(lambda: core.status_dict[_USER_STREAM] is True, timeout=5.0)
        rig.priv.drop()
        await until(lambda: core.status_dict[_USER_STREAM] is False)
        assert core.ready is False

    async def test_stream_recovers_after_a_private_socket_drop(
        self, rig: Rig, build: Callable[..., Any], seen: base.Recorder
    ) -> None:
        core = build()
        await core.start()
        await until(lambda: core.status_dict[_USER_STREAM] is True, timeout=5.0)
        first = rig.priv
        first.drop()
        await until(lambda: len(rig.privs) >= 2, timeout=5.0)  # the connector reconnected
        # A subscribe landed on the NEW socket and the flag is up again (it went down on the drop).
        await until(
            lambda: bool(rig.priv.frames("subscribe")) and core.status_dict[_USER_STREAM] is True,
            timeout=5.0,
        )
        assert rig.priv is not first
        client_id = await base._place(core, seen, "sell")
        rig.priv.push(_ws_frame("T2"))
        await seen.wait(base.FILLED)
        assert seen.of(base.FILLED)[0].order_id == client_id
