# tests/unit/transport/test_ws_base.py
"""Tests for WsConnectorBase: WsAuthModel + WsShapeDecoder integration.

Covers:
- Constructor signature with ws_auth and decoder (required)
- connect() calls prepare_connection → uses returned URL → on_connected with send callable
- send() calls transform_outgoing before underlying raw send
- Refresh loop fires at configured interval
- subscribe(channel, pair, handler) registers handler; incoming DATA frames dispatched
- Heartbeat frames absorbed silently
- Unknown channel falls back gracefully (no exception)
- Pair-less channel fallback: (channel, None) lookup when (channel, pair) absent
- Disconnect cancels the refresh task and closes WS
- Management messages (SUBSCRIBE_ACK, ERROR) land on management_messages queue
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.transport.ws_base import WsConnectorBase
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _FakeAuth:
    """WsAuthModel fake that records calls."""

    def __init__(self, rewritten_url: str = "wss://fake.example.com") -> None:
        self.rewritten_url = rewritten_url
        self.prepare_calls: list[str] = []
        self.on_connected_calls: list[Callable] = []
        self.transform_calls: list[dict] = []
        self.refresh_calls: int = 0

    async def prepare_connection(self, base_url: str) -> str:
        self.prepare_calls.append(base_url)
        return self.rewritten_url

    async def on_connected(self, ws_send: Callable) -> None:
        self.on_connected_calls.append(ws_send)

    async def transform_outgoing(self, msg: dict) -> dict:
        self.transform_calls.append(msg)
        return {**msg, "_transformed": True}

    async def refresh(self) -> None:
        self.refresh_calls += 1


class _FakeDecoder:
    """WsShapeDecoder fake: returns pre-canned NormalizedWsMessage."""

    def __init__(self) -> None:
        self._next: NormalizedWsMessage | None = None

    def prime(self, msg: NormalizedWsMessage) -> None:
        self._next = msg

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:
        if self._next is not None:
            result = self._next
            self._next = None
            return result
        # Default: UNKNOWN
        return NormalizedWsMessage(
            kind=WsMessageKind.UNKNOWN, channel=None, pair=None, payload={}, error=None
        )


class _FakeWs:
    """Minimal WS connection fake with an async-iterable frame queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.pinged = 0

    def push(self, frame: dict | list) -> None:
        self._queue.put_nowait(json.dumps(frame))

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        self.pinged += 1

    def __aiter__(self) -> _FakeWs:
        return self

    async def __anext__(self) -> str:
        # Block until a frame arrives or the queue signals done
        return await self._queue.get()


def _make_connector(
    url: str = "wss://base.example.com",
    auth: _FakeAuth | None = None,
    decoder: _FakeDecoder | None = None,
    refresh_interval: float | None = None,
    max_subscriptions: int = 0,
) -> WsConnectorBase:
    return WsConnectorBase(
        url=url,
        ws_auth=auth or _FakeAuth(),
        decoder=decoder or _FakeDecoder(),
        refresh_interval=refresh_interval,
        max_subscriptions=max_subscriptions,
    )


async def _connect_with_fake_ws(connector: WsConnectorBase, fake_ws: _FakeWs) -> None:
    """Patch websockets.connect to return fake_ws and run connect()."""
    import unittest.mock as mock

    target = "market_connector.transport.ws_base.websockets.connect"
    with mock.patch(target, AsyncMock(return_value=fake_ws)):
        await connector.connect()


# ---------------------------------------------------------------------------
# Constructor / signature
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_requires_ws_auth(self) -> None:
        decoder = _FakeDecoder()
        with pytest.raises(TypeError):
            WsConnectorBase(url="wss://x.com", decoder=decoder)  # type: ignore[call-arg]

    def test_requires_decoder(self) -> None:
        auth = _FakeAuth()
        with pytest.raises(TypeError):
            WsConnectorBase(url="wss://x.com", ws_auth=auth)  # type: ignore[call-arg]

    def test_full_signature(self) -> None:
        c = _make_connector(refresh_interval=30.0, max_subscriptions=5)
        assert c is not None

    def test_no_legacy_auth_param(self) -> None:
        """The old `auth` callable parameter must not exist."""
        auth = _FakeAuth()
        decoder = _FakeDecoder()
        with pytest.raises(TypeError):
            WsConnectorBase(url="wss://x.com", auth=lambda: None, ws_auth=auth, decoder=decoder)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# connect() lifecycle
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_prepare_connection_called_with_base_url(self) -> None:
        auth = _FakeAuth(rewritten_url="wss://rewritten.example.com")
        conn = _make_connector(url="wss://base.example.com", auth=auth)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        assert auth.prepare_calls == ["wss://base.example.com"]
        await conn.disconnect()

    async def test_connect_uses_rewritten_url(self) -> None:
        import unittest.mock as mock

        auth = _FakeAuth(rewritten_url="wss://rewritten.example.com")
        conn = _make_connector(url="wss://base.example.com", auth=auth)
        fake_ws = _FakeWs()
        connect_mock = AsyncMock(return_value=fake_ws)
        target = "market_connector.transport.ws_base.websockets.connect"
        with mock.patch(target, connect_mock):
            await conn.connect()
        connect_mock.assert_called_once_with("wss://rewritten.example.com")
        await conn.disconnect()

    async def test_on_connected_called_with_send_callable(self) -> None:
        auth = _FakeAuth()
        conn = _make_connector(auth=auth)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        assert len(auth.on_connected_calls) == 1
        send_fn = auth.on_connected_calls[0]
        assert callable(send_fn)
        await conn.disconnect()

    async def test_on_connected_send_callable_sends_json(self) -> None:
        """The send callable passed to on_connected bypasses transform_outgoing."""
        auth = _FakeAuth()
        conn = _make_connector(auth=auth)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        raw_send = auth.on_connected_calls[0]
        await raw_send({"type": "login"})
        assert fake_ws.sent == [json.dumps({"type": "login"})]
        # transform_outgoing must NOT have been called for this raw send
        assert auth.transform_calls == []
        await conn.disconnect()

    async def test_not_connected_raises_on_send(self) -> None:
        conn = _make_connector()
        with pytest.raises(GatewayNotStartedError):
            await conn.send({"type": "subscribe"})


# ---------------------------------------------------------------------------
# send() — transform_outgoing
# ---------------------------------------------------------------------------


class TestSend:
    async def test_transform_outgoing_called_before_raw_send(self) -> None:
        auth = _FakeAuth()
        conn = _make_connector(auth=auth)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        await conn.send({"type": "subscribe", "channel": "trades"})
        assert len(auth.transform_calls) == 1
        assert auth.transform_calls[0] == {"type": "subscribe", "channel": "trades"}
        # The raw-sent frame should be the transformed dict
        assert json.loads(fake_ws.sent[0]) == {
            "type": "subscribe",
            "channel": "trades",
            "_transformed": True,
        }
        await conn.disconnect()

    async def test_send_raises_when_not_connected(self) -> None:
        conn = _make_connector()
        with pytest.raises(GatewayNotStartedError):
            await conn.send({"msg": "hello"})


# ---------------------------------------------------------------------------
# Refresh loop
# ---------------------------------------------------------------------------


class TestRefreshLoop:
    async def test_refresh_fires_at_interval(self) -> None:
        auth = _FakeAuth()
        conn = _make_connector(auth=auth, refresh_interval=0.05)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        await asyncio.sleep(0.18)  # allow >=3 ticks
        await conn.disconnect()
        assert auth.refresh_calls >= 2

    async def test_no_refresh_loop_when_interval_none(self) -> None:
        auth = _FakeAuth()
        conn = _make_connector(auth=auth, refresh_interval=None)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        await asyncio.sleep(0.05)
        await conn.disconnect()
        assert auth.refresh_calls == 0

    async def test_refresh_task_cancelled_on_disconnect(self) -> None:
        auth = _FakeAuth()
        conn = _make_connector(auth=auth, refresh_interval=10.0)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        task = conn._refresh_task
        assert task is not None
        assert not task.done()
        await conn.disconnect()
        # Yield to event loop so cancellation propagates
        await asyncio.sleep(0)
        assert task.done()


# ---------------------------------------------------------------------------
# subscribe() + incoming frame routing
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_data_frame_routed_to_handler(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        received: list[Any] = []
        conn.subscribe("trades", "BTC-USD", received.append)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.DATA,
                channel="trades",
                pair="BTC-USD",
                payload={"price": "50000"},
                error=None,
            )
        )
        fake_ws.push({"channel": "trades", "pair": "BTC-USD"})
        await asyncio.sleep(0.05)
        await conn.disconnect()
        assert received == [{"price": "50000"}]

    async def test_pair_none_fallback(self) -> None:
        """Handler registered as (channel, None) receives frames for any pair."""
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        received: list[Any] = []
        conn.subscribe("status", None, received.append)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.DATA,
                channel="status",
                pair="ETH-USD",
                payload={"status": "ok"},
                error=None,
            )
        )
        fake_ws.push({"channel": "status"})
        await asyncio.sleep(0.05)
        await conn.disconnect()
        assert received == [{"status": "ok"}]

    async def test_heartbeat_absorbed_silently(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        received: list[Any] = []
        conn.subscribe("heartbeat", None, received.append)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.HEARTBEAT,
                channel="heartbeat",
                pair=None,
                payload={},
                error=None,
            )
        )
        fake_ws.push({"type": "heartbeat"})
        await asyncio.sleep(0.05)
        await conn.disconnect()
        assert received == []

    async def test_unknown_frame_dropped_no_exception(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.UNKNOWN,
                channel=None,
                pair=None,
                payload={},
                error=None,
            )
        )
        fake_ws.push({"garbage": True})
        await asyncio.sleep(0.05)
        await conn.disconnect()  # no exception raised

    async def test_unregistered_channel_dropped_no_exception(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.DATA,
                channel="unregistered",
                pair="BTC-USD",
                payload={"x": 1},
                error=None,
            )
        )
        fake_ws.push({"channel": "unregistered"})
        await asyncio.sleep(0.05)
        await conn.disconnect()  # no exception raised

    async def test_subscribe_ack_queued_to_management(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.SUBSCRIBE_ACK,
                channel="trades",
                pair=None,
                payload={"status": "subscribed"},
                error=None,
            )
        )
        fake_ws.push({"type": "subscriptions"})
        await asyncio.sleep(0.05)
        msg = conn.management_messages.get_nowait()
        assert msg.kind == WsMessageKind.SUBSCRIBE_ACK
        await conn.disconnect()

    async def test_error_frame_queued_to_management(self) -> None:
        decoder = _FakeDecoder()
        conn = _make_connector(decoder=decoder)
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)

        decoder.prime(
            NormalizedWsMessage(
                kind=WsMessageKind.ERROR,
                channel=None,
                pair=None,
                payload={},
                error="rate limit exceeded",
            )
        )
        fake_ws.push({"type": "error"})
        await asyncio.sleep(0.05)
        msg = conn.management_messages.get_nowait()
        assert msg.kind == WsMessageKind.ERROR
        assert msg.error == "rate limit exceeded"
        await conn.disconnect()


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_disconnect_closes_ws(self) -> None:
        conn = _make_connector()
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        await conn.disconnect()
        assert fake_ws.closed

    async def test_disconnect_cancels_listen_task(self) -> None:
        conn = _make_connector()
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        task = conn._listen_task
        await conn.disconnect()
        # Yield to event loop so cancellation propagates
        await asyncio.sleep(0)
        assert task is not None and task.done()

    async def test_double_disconnect_safe(self) -> None:
        conn = _make_connector()
        fake_ws = _FakeWs()
        await _connect_with_fake_ws(conn, fake_ws)
        await conn.disconnect()
        await conn.disconnect()  # must not raise
