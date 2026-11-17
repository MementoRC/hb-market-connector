"""Kraken private WebSocket wiring + ack-gated private subscribes (issue #274), RED step.

Pinned contract (nothing here exists yet):

* G1 ``KrakenGateway._ws_private``: a second ``WsConnectorBase`` on ``config.ws_auth_url`` using
  ``_ws_private_auth`` and the same decoder / reconnect settings as the public ``_ws``.
* G2 The token fetch goes through the REAL REST API: ``_rest.request("get_websockets_token")``
  then ``.raw["result"]["token"]``, with ``_rest`` resolved at call time. The REST double used
  here has ONLY ``request`` / ``close`` (no ``.get``, like the real transport).
* G3 ``start()`` = server_time -> public connect -> private connect -> started. A failing private
  connect raises and disconnects the public connector. ``stop()`` = private, public, REST close.
  (Credentials are always required, see ``kraken_signer_factory``: no credential-less case.)
* G4 ``subscribe_own_trades`` / ``subscribe_open_orders`` register and send on ``_ws_private``;
  order book / trades stay on ``_ws``.
* G5 Private subscribe frames carry a unique, monotonic int ``reqid``; entering the context
  returns only when a SUBSCRIBE_ACK with that reqid and status ``subscribed`` arrives. Status
  ``error`` -> ``SubscriptionRejectedError``; no ack within ``KrakenConfig.subscribe_ack_timeout``
  (default 10.0, read from ``gateway._config`` at subscribe time) -> ``SubscriptionAckTimeoutError``
  (also a ``TimeoutError``). Both are ``GatewayError`` subclasses (``market_connector.exceptions``)
  and remove the route. Unknown / missing reqid acks are ignored; the private
  ``management_messages`` queue is drained; unsubscribe on exit stays best-effort, not gated.
* G6 ``wait_ws_disconnect()`` reflects the PRIVATE connector.

The sockets are fakes behind ``websockets.connect``; everything else (gateway, connectors,
auth, decoder, mixin) runs for real. Public helpers (``open_rig`` etc.) are reused by the other
#274 test modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from market_connector.exchanges.kraken.kraken_gateway import KrakenConfig, KrakenGateway
from market_connector.exchanges.kraken.specs import KRAKEN_WS_DECODER
from market_connector.transport.response import Response
from market_connector.transport.ws_base import WsConnectorBase
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind
from tests.unit.transport import test_ws_base as wsb

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_CONNECT_TARGET = "market_connector.transport.ws_base.websockets.connect"
_OWN_TRADES = "ownTrades"
_OPEN_ORDERS = "openOrders"
_PERMISSION_DENIED = "EGeneral:Permission denied"


# ---------------------------------------------------------------------------
# Shared fakes and helpers (imported by the other #274 modules)
# ---------------------------------------------------------------------------


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def extra_reply(endpoint_name: str, token: str) -> Response | None:
    """Replies for the REST endpoints the gateway lifecycle uses, or ``None`` if not ours."""
    if endpoint_name == "server_time":
        raw: dict[str, Any] = {"error": [], "result": {"unixtime": 1700000000, "rfc1123": "x"}}
    elif endpoint_name == "get_websockets_token":
        raw = {"error": [], "result": {"token": token, "expires": 900}}
    else:
        return None
    return Response(raw=raw, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


class TokenRest:
    """REST double with ONLY ``request`` and ``close``, like the real transport (no ``.get``)."""

    def __init__(self, token: str | None = None, log: list[str] | None = None) -> None:
        self.token = token or f"tok-{uuid.uuid4().hex}"
        self.calls: list[str] = []
        self.closed = False
        self._log = log

    async def request(self, endpoint_name: str, *_: Any, **__: Any) -> Response:
        self.calls.append(endpoint_name)
        if self._log is not None:
            self._log.append(f"rest:{endpoint_name}")
        reply = extra_reply(endpoint_name, self.token)
        if reply is None:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return reply

    async def close(self) -> None:
        self.closed = True
        if self._log is not None:
            self._log.append("rest:close")


def subscription_ack(
    frame: dict[str, Any], status: str = "subscribed", error_message: str | None = None
) -> dict[str, Any]:
    """The Kraken ``subscriptionStatus`` reply to a subscribe *frame* (echoing its reqid)."""
    name = frame["subscription"]["name"]
    reply: dict[str, Any] = {
        "event": "subscriptionStatus",
        "status": status,
        "reqid": frame.get("reqid"),
        "subscription": {"name": name},
    }
    if status == "error":
        reply["errorMessage"] = error_message or _PERMISSION_DENIED
    else:
        reply["channelName"] = name
    return reply


def auto_ack(frame: dict[str, Any]) -> dict[str, Any] | None:
    return subscription_ack(frame)


def silent(_frame: dict[str, Any]) -> dict[str, Any] | None:
    return None


def reject(frame: dict[str, Any]) -> dict[str, Any] | None:
    return subscription_ack(frame, "error")


class PrivWs(wsb._FakeWs):
    """Private-socket fake: answers each subscribe frame via ``rig.responder``; can be dropped."""

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self._rig = rig
        self.fail_sends = False

    def frames(self, event: str) -> list[dict[str, Any]]:
        parsed = [json.loads(raw) for raw in self.sent]
        return [f for f in parsed if f.get("event") == event]

    async def send(self, data: str) -> None:
        if self.fail_sends:
            raise ConnectionError("send failed")
        await super().send(data)
        frame = json.loads(data)
        if frame.get("event") == "subscribe":
            reply = self._rig.responder(frame)
            if reply is not None:
                self.push(reply)

    def drop(self) -> None:
        self._queue.put_nowait("__drop__")

    async def __anext__(self) -> str:
        frame = await self._queue.get()
        if frame == "__drop__":
            raise ConnectionError("connection dropped")
        return frame


class RawFrameDecoder:
    """Delivers array frames to their handler as the raw frame (what the mixin handlers decode)."""

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:
        if isinstance(raw, dict):
            return KRAKEN_WS_DECODER.decode(raw)
        assert isinstance(raw, list)
        return NormalizedWsMessage(
            kind=WsMessageKind.DATA, channel=raw[-2], pair=None, payload=raw, error=None
        )


@dataclass
class Rig:
    gw: KrakenGateway
    rest: Any
    pub: wsb._FakeWs
    privs: list[PrivWs] = field(default_factory=list)
    responder: Callable[[dict[str, Any]], dict[str, Any] | None] = auto_ack

    @property
    def priv(self) -> PrivWs:
        """The latest private socket."""
        return self.privs[-1]

    @property
    def conn(self) -> WsConnectorBase:
        return self.gw._ws_private  # type: ignore[attr-defined,no-any-return]

    def use_raw_frame_decoder(self) -> None:
        self.conn._decoder = RawFrameDecoder()  # type: ignore[assignment]


@contextlib.asynccontextmanager
async def open_rig(
    monkeypatch: pytest.MonkeyPatch,
    rest: Any = None,
    *,
    ack_timeout: float | None = None,
    private_reconnect_delay: float | None = None,
) -> AsyncIterator[Rig]:
    """A real ``KrakenGateway`` started through ``start()`` over fake public/private sockets."""
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest if rest is not None else TokenRest()
    rig = Rig(gw=gw, rest=gw._rest, pub=wsb._FakeWs())

    async def _connect(url: str, *_: Any, **__: Any) -> Any:
        if "ws-auth" not in url:
            return rig.pub
        sock = PrivWs(rig)
        rig.privs.append(sock)
        return sock

    monkeypatch.setattr(_CONNECT_TARGET, _connect)
    if ack_timeout is not None:
        gw._config = dataclasses.replace(gw._config, subscribe_ack_timeout=ack_timeout)  # type: ignore[call-arg]
    if private_reconnect_delay is not None:
        rig.conn._reconnect_delay = private_reconnect_delay
    await gw.start()
    try:
        yield rig
    finally:
        await gw.stop()


def _noop(_event: Any) -> None:
    return None


async def enter(subscribe: Callable[..., Any], callback: Callable[[Any], None] = _noop) -> Any:
    """``async with await subscribe(cb)`` split in two so a test can watch it block."""
    ctx = await subscribe(callback)
    await ctx.__aenter__()
    return ctx


async def leave(ctx: Any) -> None:
    await ctx.__aexit__(None, None, None)


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    async with open_rig(monkeypatch) as opened:
        yield opened


# ---------------------------------------------------------------------------
# G1. The private connector
# ---------------------------------------------------------------------------


class TestPrivateConnector:
    def test_gateway_has_a_distinct_private_connector_on_the_auth_url(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        private = gw._ws_private  # type: ignore[attr-defined]
        assert isinstance(private, WsConnectorBase)
        assert private is not gw._ws
        assert private._url == gw._config.ws_auth_url
        assert gw._ws._url == gw._config.ws_url
        assert private._url != gw._ws._url

    def test_private_connector_uses_the_private_auth(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        assert gw._ws_private._ws_auth is gw._ws_private_auth  # type: ignore[attr-defined]
        assert gw._ws._ws_auth is gw._ws_auth
        assert gw._ws_private_auth is not gw._ws_auth

    def test_private_connector_shares_decoder_and_reconnect_settings(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        private = gw._ws_private  # type: ignore[attr-defined]
        assert private._decoder is gw._ws._decoder
        assert private._reconnect_delay == gw._ws._reconnect_delay
        assert private._max_reconnect_delay == gw._ws._max_reconnect_delay
        assert private._heartbeat_interval == gw._ws._heartbeat_interval


# ---------------------------------------------------------------------------
# G2. Token source: the real REST API, resolved at call time
# ---------------------------------------------------------------------------


class TestTokenSource:
    async def test_token_comes_from_request_get_websockets_token(self, rig: Rig) -> None:
        assert not hasattr(rig.rest, "get")  # the double is as narrow as the real transport
        assert "get_websockets_token" in rig.rest.calls  # fetched when the private WS connected
        ctx = await enter(rig.gw.subscribe_own_trades)
        try:
            raw = next(s for s in rig.priv.sent if json.loads(s).get("event") == "subscribe")
            assert rig.rest.token in raw
        finally:
            await leave(ctx)

    async def test_rest_swapped_after_construction_is_used_on_refresh(self, rig: Rig) -> None:
        later = TokenRest()
        rig.gw._rest = later
        await rig.gw._ws_private_auth.refresh()
        assert later.calls == ["get_websockets_token"]
        ctx = await enter(rig.gw.subscribe_own_trades)
        try:
            frames = [s for s in rig.priv.sent if json.loads(s).get("event") == "subscribe"]
            assert later.token in frames[-1]
        finally:
            await leave(ctx)


# ---------------------------------------------------------------------------
# G3. Lifecycle ordering
# ---------------------------------------------------------------------------


def _instrument(
    monkeypatch: pytest.MonkeyPatch, gw: KrakenGateway, log: list[str], *, private_fails: bool
) -> None:
    """Replace both connectors' connect/disconnect with log-recording fakes."""
    for attr, label in (("_ws", "ws"), ("_ws_private", "priv")):
        conn = getattr(gw, attr)

        async def _connect(label: str = label) -> None:
            log.append(f"{label}:connect")
            if private_fails and label == "priv":
                raise ConnectionError("private connect failed")

        async def _disconnect(label: str = label) -> None:
            log.append(f"{label}:disconnect")

        monkeypatch.setattr(conn, "connect", _connect)
        monkeypatch.setattr(conn, "disconnect", _disconnect)


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> tuple[KrakenGateway, list[str]]:
    log: list[str] = []
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = TokenRest(log=log)  # type: ignore[assignment]
    _instrument(monkeypatch, gw, log, private_fails=False)
    return gw, log


class TestLifecycle:
    async def test_start_connects_server_time_then_public_then_private(
        self, lifecycle: tuple[KrakenGateway, list[str]]
    ) -> None:
        gw, log = lifecycle
        assert gw.ready is False
        await gw.start()
        assert log == ["rest:server_time", "ws:connect", "priv:connect"]
        assert gw.ready is True

    async def test_stop_disconnects_private_then_public_then_closes_rest(
        self, lifecycle: tuple[KrakenGateway, list[str]]
    ) -> None:
        gw, log = lifecycle
        await gw.start()
        log.clear()
        await gw.stop()
        assert log == ["priv:disconnect", "ws:disconnect", "rest:close"]
        assert gw.ready is False

    async def test_private_connect_failure_raises_and_disconnects_the_public_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log: list[str] = []
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        gw._rest = TokenRest(log=log)  # type: ignore[assignment]
        _instrument(monkeypatch, gw, log, private_fails=True)
        with pytest.raises(ConnectionError, match="private connect failed"):
            await gw.start()
        assert gw.ready is False
        assert log.index("ws:connect") < log.index("priv:connect") < log.index("ws:disconnect")

    async def test_start_is_idempotent_once_started(
        self, lifecycle: tuple[KrakenGateway, list[str]]
    ) -> None:
        gw, log = lifecycle
        await gw.start()
        await gw.start()
        assert log.count("priv:connect") == 1
        assert log.count("ws:connect") == 1


# ---------------------------------------------------------------------------
# G4. Routing: private channels on the private connector, market data on the public one
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_private_subscribes_register_and_send_on_the_private_connector(
        self, rig: Rig
    ) -> None:
        own = await enter(rig.gw.subscribe_own_trades)
        opn = await enter(rig.gw.subscribe_open_orders)
        try:
            names = [f["subscription"]["name"] for f in rig.priv.frames("subscribe")]
            assert names == [_OWN_TRADES, _OPEN_ORDERS]
            assert (_OWN_TRADES, None) in rig.conn._routing_table
            assert (_OPEN_ORDERS, None) in rig.conn._routing_table
            assert (_OWN_TRADES, None) not in rig.gw._ws._routing_table
            assert (_OPEN_ORDERS, None) not in rig.gw._ws._routing_table
            assert rig.pub.sent == []
        finally:
            await leave(opn)
            await leave(own)

    async def test_market_data_subscribes_stay_on_the_public_connector(self, rig: Rig) -> None:
        book = await rig.gw.subscribe_orderbook("XBT/USD", _noop, depth=10)
        trades = await rig.gw.subscribe_trades("XBT/USD", _noop)
        async with book, trades:
            sent = [json.loads(raw) for raw in rig.pub.sent]
            assert [f["subscription"]["name"] for f in sent if f["event"] == "subscribe"] == [
                "book",
                "trade",
            ]
            assert rig.priv.sent == []
            assert ("trade", "XBT/USD") in rig.gw._ws._routing_table
            assert ("trade", "XBT/USD") not in rig.conn._routing_table


# ---------------------------------------------------------------------------
# G5. Ack gating
# ---------------------------------------------------------------------------


class TestReqid:
    async def test_every_private_subscribe_carries_a_unique_increasing_int_reqid(
        self, rig: Rig
    ) -> None:
        for _ in range(3):
            for subscribe in (rig.gw.subscribe_own_trades, rig.gw.subscribe_open_orders):
                await leave(await enter(subscribe))
        reqids = [f["reqid"] for f in rig.priv.frames("subscribe")]
        assert len(reqids) == 6
        assert all(isinstance(r, int) and not isinstance(r, bool) for r in reqids)
        assert all(a < b for a, b in zip(reqids, reqids[1:], strict=False))

    async def test_default_ack_timeout_is_ten_seconds(self) -> None:
        assert KrakenConfig(api_key="k", secret_key="s").subscribe_ack_timeout == 10.0  # type: ignore[attr-defined]
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        assert gw._config.subscribe_ack_timeout == 10.0  # type: ignore[attr-defined]


class TestAckGate:
    async def test_enter_blocks_until_the_matching_ack(self, rig: Rig) -> None:
        rig.responder = silent
        task = asyncio.create_task(enter(rig.gw.subscribe_own_trades))
        try:
            await until(lambda: len(rig.priv.frames("subscribe")) == 1)
            await asyncio.sleep(0.05)
            assert not task.done()
            rig.priv.push(subscription_ack(rig.priv.frames("subscribe")[0]))
            await leave(await asyncio.wait_for(task, 2.0))
        finally:
            task.cancel()

    async def test_unknown_or_missing_reqid_acks_are_ignored(self, rig: Rig) -> None:
        rig.responder = silent
        task = asyncio.create_task(enter(rig.gw.subscribe_own_trades))
        try:
            await until(lambda: len(rig.priv.frames("subscribe")) == 1)
            frame = rig.priv.frames("subscribe")[0]
            other = {**frame, "reqid": frame["reqid"] + 10_000}
            rig.priv.push(subscription_ack(other))
            rig.priv.push(subscription_ack(other, "error"))
            no_reqid = subscription_ack(frame)
            del no_reqid["reqid"]
            rig.priv.push(no_reqid)
            await asyncio.sleep(0.05)
            assert not task.done()  # still waiting for ITS ack, neither resolved nor rejected
            assert rig.conn._listen_task is not None
            assert not rig.conn._listen_task.done()
            rig.priv.push(subscription_ack(frame))
            await leave(await asyncio.wait_for(task, 2.0))
        finally:
            task.cancel()

    async def test_concurrent_subscribes_resolve_from_their_own_acks(self, rig: Rig) -> None:
        rig.responder = silent
        tasks = {
            _OWN_TRADES: asyncio.create_task(enter(rig.gw.subscribe_own_trades)),
            _OPEN_ORDERS: asyncio.create_task(enter(rig.gw.subscribe_open_orders)),
        }
        try:
            await until(lambda: len(rig.priv.frames("subscribe")) == 2)
            first, last = rig.priv.frames("subscribe")
            first_name, last_name = first["subscription"]["name"], last["subscription"]["name"]
            rig.priv.push(subscription_ack(last))  # acks arrive in reverse order
            await until(lambda: tasks[last_name].done())
            await asyncio.sleep(0.05)
            assert not tasks[first_name].done()
            rig.priv.push(subscription_ack(first))
            ctxs = [await asyncio.wait_for(t, 2.0) for t in tasks.values()]
            for ctx in ctxs:
                await leave(ctx)
        finally:
            for task in tasks.values():
                task.cancel()

    async def test_management_queue_does_not_grow_without_bound(self, rig: Rig) -> None:
        for _ in range(10):
            await leave(await enter(rig.gw.subscribe_own_trades))
        for reqid in range(900_000, 900_005):
            rig.priv.push(subscription_ack({"subscription": {"name": _OWN_TRADES}, "reqid": reqid}))
        await until(lambda: rig.conn.management_messages.qsize() == 0)


class TestRejection:
    @pytest.mark.parametrize("subscribe_name", ["subscribe_own_trades", "subscribe_open_orders"])
    async def test_error_ack_raises_subscription_rejected_and_removes_the_route(
        self, rig: Rig, subscribe_name: str
    ) -> None:
        from market_connector.exceptions import GatewayError, SubscriptionRejectedError

        assert issubclass(SubscriptionRejectedError, GatewayError)
        rig.responder = reject
        with pytest.raises(SubscriptionRejectedError) as raised:
            await asyncio.wait_for(enter(getattr(rig.gw, subscribe_name)), 2.0)
        assert _PERMISSION_DENIED in str(raised.value)
        assert len(rig.conn._routing_table) == 0

    async def test_gateway_recovers_after_a_rejection(self, rig: Rig) -> None:
        from market_connector.exceptions import SubscriptionRejectedError

        rig.responder = reject
        with pytest.raises(SubscriptionRejectedError):
            await asyncio.wait_for(enter(rig.gw.subscribe_own_trades), 2.0)
        rig.responder = auto_ack
        ctx = await asyncio.wait_for(enter(rig.gw.subscribe_own_trades), 2.0)
        assert (_OWN_TRADES, None) in rig.conn._routing_table
        await leave(ctx)


class TestAckTimeout:
    async def test_no_ack_raises_timeout_error_and_removes_the_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from market_connector.exceptions import GatewayError, SubscriptionAckTimeoutError

        assert issubclass(SubscriptionAckTimeoutError, GatewayError)
        assert issubclass(SubscriptionAckTimeoutError, TimeoutError)
        async with open_rig(monkeypatch, ack_timeout=0.05) as rig:
            rig.responder = silent
            with pytest.raises(SubscriptionAckTimeoutError):
                await asyncio.wait_for(enter(rig.gw.subscribe_open_orders), 2.0)
            assert len(rig.conn._routing_table) == 0

    async def test_late_ack_after_timeout_is_ignored_and_gateway_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with open_rig(monkeypatch, ack_timeout=0.05) as rig:
            rig.responder = silent
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(enter(rig.gw.subscribe_own_trades), 2.0)
            rig.priv.push(subscription_ack(rig.priv.frames("subscribe")[0]))  # too late
            await asyncio.sleep(0.05)
            assert not rig.conn._listen_task.done()  # type: ignore[union-attr]
            rig.responder = auto_ack
            await leave(await asyncio.wait_for(enter(rig.gw.subscribe_own_trades), 2.0))


class TestUnsubscribe:
    async def test_exit_sends_unsubscribe_without_waiting_for_any_ack(self, rig: Rig) -> None:
        ctx = await enter(rig.gw.subscribe_own_trades)
        await asyncio.wait_for(leave(ctx), 1.0)  # the fake never acks unsubscribes
        (unsub,) = rig.priv.frames("unsubscribe")
        assert unsub["subscription"]["name"] == _OWN_TRADES
        assert len(rig.conn._routing_table) == 0

    async def test_exit_is_best_effort_when_the_unsubscribe_send_fails(self, rig: Rig) -> None:
        ctx = await enter(rig.gw.subscribe_open_orders)
        rig.priv.fail_sends = True
        await asyncio.wait_for(leave(ctx), 1.0)  # must not raise
        assert len(rig.conn._routing_table) == 0


# ---------------------------------------------------------------------------
# G6. wait_ws_disconnect reflects the private connector
# ---------------------------------------------------------------------------


class TestWaitWsDisconnect:
    async def test_public_drop_does_not_release_the_waiter_but_private_drop_does(
        self, rig: Rig
    ) -> None:
        waiter = asyncio.create_task(rig.gw.wait_ws_disconnect())
        try:
            rig.gw._ws._dropped.set()  # the public connection "drops"
            await asyncio.sleep(0.1)
            assert not waiter.done()
            rig.priv.drop()
            await asyncio.wait_for(waiter, 1.0)
        finally:
            waiter.cancel()
