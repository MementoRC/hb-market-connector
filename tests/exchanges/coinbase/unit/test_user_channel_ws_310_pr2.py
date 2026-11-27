"""Acceptance tests for #310 PR 2: Coinbase user-channel WS subscription (RED step).

Assumed public API on ``CoinbaseGateway`` (does not exist yet)::

    async def subscribe_user_orders(
        self,
        callback: Callable[[UserEvent], None],
        *,
        product_ids: Sequence[str] | None = None,
        on_snapshot_complete: Callable[[], None] | None = None,
        on_gap: Callable[[int, int], None] | None = None,
    ) -> AbstractAsyncContextManager[None]

* ``callback`` is called once per ``UserEvent`` (snapshot and update) of each ``user`` message.
* ``on_snapshot_complete()`` fires once the first snapshot batch with fewer than 50 orders arrives.
* ``on_gap(expected, received)`` fires when ``sequence_num`` jumps within one connection.
* Entering the context opens the user socket (``config.ws_user_url``) and sends the subscribes;
  leaving it sends the unsubscribe.

Sockets are fakes behind ``websockets.connect``; no network. The rest of the gateway is real.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.transport.response import Response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_CONNECT_TARGET = "market_connector.transport.ws_base.websockets.connect"
API_KEY = "organizations/org-1/apiKeys/key-1"
SNAPSHOT_BATCH = 50


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


class FakeWs:
    """Async-iterable fake socket; ``drop()`` ends the iteration like a lost connection."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[str] = []
        self.closed = False
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def push(self, frame: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(frame))

    def drop(self) -> None:
        self._queue.put_nowait(None)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        return None

    def __aiter__(self) -> FakeWs:
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def frames(self, channel: str, type_: str = "subscribe") -> list[dict[str, Any]]:
        parsed = [json.loads(s) for s in self.sent]
        return [f for f in parsed if f.get("type") == type_ and f.get("channel") == channel]


class Rig:
    def __init__(self, gateway: CoinbaseGateway, public_pem: str) -> None:
        self.gateway = gateway
        self.public_pem = public_pem
        self.sockets: dict[str, list[FakeWs]] = defaultdict(list)

    def connect(self, url: str, *_: Any, **__: Any) -> FakeWs:
        ws = FakeWs(url)
        self.sockets[url].append(ws)
        return ws

    @property
    def user_url(self) -> str:
        return self.gateway._config.ws_user_url

    def user_sockets(self) -> list[FakeWs]:
        return self.sockets[self.user_url]

    async def user_socket(self, index: int = 0) -> FakeWs:
        await until(lambda: len(self.user_sockets()) > index)
        return self.user_sockets()[index]

    async def subscribed_socket(self, index: int = 0) -> FakeWs:
        """The index-th user socket once both the user and heartbeats subscribes went out."""
        ws = await self.user_socket(index)
        await until(lambda: bool(ws.frames("user")) and bool(ws.frames("heartbeats")))
        return ws

    def verify(self, token: str) -> dict[str, Any]:
        return pyjwt.decode(
            token, self.public_pem, algorithms=["ES256"], options={"verify_aud": False}
        )


@pytest.fixture(scope="module")
def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
async def rig(ec_key: ec.EllipticCurvePrivateKey) -> AsyncIterator[Rig]:
    private_pem = ec_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        ec_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    gateway = CoinbaseGateway(CoinbaseConfig(api_key=API_KEY, secret_key=private_pem))
    the_rig = Rig(gateway, public_pem)
    with (
        patch.object(gateway._rest, "request", new=AsyncMock(return_value=Response(raw={}))),
        patch.object(gateway._rest, "close", new=AsyncMock()),
        patch(_CONNECT_TARGET, AsyncMock(side_effect=the_rig.connect)),
    ):
        await gateway.start()
        try:
            yield the_rig
        finally:
            await gateway.stop()


def order(order_id: str = "o-1", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": order_id,
        "client_order_id": "c-1",
        "product_id": "BTC-USD",
        "cumulative_quantity": "0.005",
        "leaves_quantity": "0.005",
        "avg_price": "50000.00",
        "number_of_fills": "1",
        "filled_value": "250.00",
        "total_fees": "0.025",
        "status": "OPEN",
        "creation_time": "2026-04-24T11:00:00Z",
        "order_side": "BUY",
        "order_type": "Limit",
        "limit_price": "50000.00",
        "stop_price": "0",
    }
    base.update(overrides)
    return base


def user_msg(seq: int, event_type: str = "update", n_orders: int = 1) -> dict[str, Any]:
    orders = [order(f"o-{seq}-{i}") for i in range(n_orders)]
    return {
        "channel": "user",
        "client_id": "",
        "timestamp": "2026-04-24T12:00:00.123456789Z",
        "sequence_num": seq,
        "events": [{"type": event_type, "orders": orders}],
    }


def heartbeat_msg(seq: int) -> dict[str, Any]:
    return {
        "channel": "heartbeats",
        "client_id": "",
        "timestamp": "2026-04-24T12:00:00.5Z",
        "sequence_num": seq,
        "events": [{"current_time": "2026-04-24 12:00:00.5 +0000 UTC", "heartbeat_counter": "9"}],
    }


class Sink:
    """Collects everything the three public callbacks deliver."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.snapshot_complete = 0
        self.gaps: list[tuple[int, int]] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)

    def on_snapshot_complete(self) -> None:
        self.snapshot_complete += 1

    def on_gap(self, expected: int, received: int) -> None:
        self.gaps.append((expected, received))


async def subscribe(rig: Rig, sink: Sink, **kwargs: Any) -> Any:
    return await rig.gateway.subscribe_user_orders(
        sink.on_event,
        on_snapshot_complete=sink.on_snapshot_complete,
        on_gap=sink.on_gap,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Wire subscribe: user + heartbeats, one channel per frame, fresh JWT each
# ---------------------------------------------------------------------------


async def test_subscribe_targets_user_url_with_user_and_heartbeats_frames(rig: Rig) -> None:
    async with await subscribe(rig, Sink()):
        ws = await rig.subscribed_socket()

        assert ws.url == rig.user_url
        user_frame = ws.frames("user")[0]
        heartbeat_frame = ws.frames("heartbeats")[0]
        assert user_frame["type"] == "subscribe"
        assert heartbeat_frame["type"] == "subscribe"
        assert user_frame["channel"] == "user"
        assert heartbeat_frame["channel"] == "heartbeats"
        assert "channels" not in user_frame
        assert "channels" not in heartbeat_frame


async def test_user_subscribe_is_sent_without_waiting_for_any_server_message(rig: Rig) -> None:
    # Coinbase disconnects sockets that do not subscribe within 5s of connecting, so
    # the subscribes must not depend on a server frame (nothing is pushed here).
    async with await subscribe(rig, Sink()):
        ws = await rig.user_socket()

        await until(lambda: bool(ws.frames("user")) and bool(ws.frames("heartbeats")), timeout=2.0)


async def test_user_subscribe_is_not_sent_on_the_public_market_data_socket(rig: Rig) -> None:
    async with await subscribe(rig, Sink()):
        await rig.subscribed_socket()

        public = rig.sockets[rig.gateway._config.ws_url]
        assert all(not ws.frames("user") for ws in public)


async def test_product_ids_are_passed_through_when_given(rig: Rig) -> None:
    async with await subscribe(rig, Sink(), product_ids=["BTC-USD", "ETH-USDC"]):
        ws = await rig.subscribed_socket()

        assert ws.frames("user")[0]["product_ids"] == ["BTC-USD", "ETH-USDC"]


async def test_product_ids_are_omitted_to_mean_all_products(rig: Rig) -> None:
    async with await subscribe(rig, Sink()):
        ws = await rig.subscribed_socket()

        assert "product_ids" not in ws.frames("user")[0]


async def test_each_subscribe_frame_carries_its_own_fresh_valid_jwt(rig: Rig) -> None:
    async with await subscribe(rig, Sink()):
        ws = await rig.subscribed_socket()

        user_jwt = ws.frames("user")[0]["jwt"]
        heartbeat_jwt = ws.frames("heartbeats")[0]["jwt"]
        assert user_jwt != heartbeat_jwt
        for token in (user_jwt, heartbeat_jwt):
            claims = rig.verify(token)
            assert claims["sub"] == API_KEY
            assert "uri" not in claims
            assert claims["exp"] - claims["nbf"] == 120
        nonces = {pyjwt.get_unverified_header(t)["nonce"] for t in (user_jwt, heartbeat_jwt)}
        assert len(nonces) == 2


# ---------------------------------------------------------------------------
# 2. Reconnect: resubscribe with a fresh JWT
# ---------------------------------------------------------------------------


async def test_reconnect_resubscribes_user_and_heartbeats_with_fresh_jwts(rig: Rig) -> None:
    async with await subscribe(rig, Sink(), product_ids=["BTC-USD"]):
        first = await rig.subscribed_socket(0)
        first.drop()

        second = await rig.subscribed_socket(1)

        assert second is not first
        assert second.url == rig.user_url
        assert second.frames("user")[0]["product_ids"] == ["BTC-USD"]
        old = {first.frames("user")[0]["jwt"], first.frames("heartbeats")[0]["jwt"]}
        new = {second.frames("user")[0]["jwt"], second.frames("heartbeats")[0]["jwt"]}
        assert old.isdisjoint(new)
        for token in new:
            rig.verify(token)


# ---------------------------------------------------------------------------
# 3. Unsubscribe carries a jwt
# ---------------------------------------------------------------------------


async def test_unsubscribe_frame_carries_a_valid_jwt(rig: Rig) -> None:
    async with await subscribe(rig, Sink()):
        ws = await rig.subscribed_socket()

    unsubscribes = ws.frames("user", type_="unsubscribe")
    assert len(unsubscribes) == 1
    claims = rig.verify(unsubscribes[0]["jwt"])
    assert claims["sub"] == API_KEY
    assert "uri" not in claims


# ---------------------------------------------------------------------------
# 4/7. Routing: user data reaches the callback; heartbeats do not
# ---------------------------------------------------------------------------


async def test_user_messages_reach_the_callback_as_user_events(rig: Rig) -> None:
    from market_connector.exchanges.coinbase.schemas.ws import UserEvent

    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1, "snapshot", n_orders=2))
        ws.push(user_msg(2, "update", n_orders=1))
        await until(lambda: len(sink.events) == 2)

    assert all(isinstance(e, UserEvent) for e in sink.events)
    assert [e.type for e in sink.events] == ["snapshot", "update"]
    assert [len(e.orders) for e in sink.events] == [2, 1]
    assert sink.events[1].orders[0].order_id == "o-2-0"
    assert sink.events[1].orders[0].cumulative_quantity == "0.005"


async def test_heartbeats_are_not_delivered_as_user_data(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(heartbeat_msg(1))
        ws.push(heartbeat_msg(2))
        ws.push(user_msg(3))
        await until(lambda: len(sink.events) == 1)

    assert sink.events[0].orders[0].order_id == "o-3-0"
    assert sink.snapshot_complete == 0


async def test_user_message_with_empty_client_order_id_is_still_delivered(rig: Rig) -> None:
    sink = Sink()
    msg = user_msg(1)
    msg["events"][0]["orders"][0]["client_order_id"] = ""
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(msg)
        await until(lambda: len(sink.events) == 1)

    assert sink.events[0].orders[0].order_id == "o-1-0"


# ---------------------------------------------------------------------------
# 5. Snapshot batching: the first batch with < 50 orders completes the snapshot
# ---------------------------------------------------------------------------


async def test_full_batches_do_not_complete_the_snapshot_a_short_batch_does(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1, "snapshot", n_orders=SNAPSHOT_BATCH))
        ws.push(user_msg(2, "snapshot", n_orders=SNAPSHOT_BATCH))
        await until(lambda: len(sink.events) == 2)
        assert sink.snapshot_complete == 0

        ws.push(user_msg(3, "snapshot", n_orders=3))
        await until(lambda: len(sink.events) == 3)

    assert sink.snapshot_complete == 1


async def test_empty_snapshot_completes_immediately(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1, "snapshot", n_orders=0))
        await until(lambda: len(sink.events) == 1)

    assert sink.snapshot_complete == 1


async def test_updates_after_the_snapshot_do_not_signal_completion_again(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1, "snapshot", n_orders=1))
        ws.push(user_msg(2, "update", n_orders=1))
        ws.push(user_msg(3, "update", n_orders=1))
        await until(lambda: len(sink.events) == 3)

    assert sink.snapshot_complete == 1


async def test_update_events_never_complete_a_snapshot(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1, "update", n_orders=1))
        await until(lambda: len(sink.events) == 1)

    assert sink.snapshot_complete == 0


@pytest.mark.skip(
    reason="needs live probe (#310): docs do not say what a last batch of exactly 50 means"
)
async def test_snapshot_whose_last_batch_has_exactly_50_orders() -> None:
    """Deliberately unasserted: completion signal when the final batch is exactly 50 orders."""


# ---------------------------------------------------------------------------
# 6. sequence_num gap detection is per connection
# ---------------------------------------------------------------------------


async def test_consecutive_sequence_numbers_report_no_gap(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        for seq in (10, 11, 12):
            ws.push(user_msg(seq))
        await until(lambda: len(sink.events) == 3)

    assert sink.gaps == []


async def test_a_sequence_jump_reports_expected_and_received(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(10))
        ws.push(user_msg(11))
        ws.push(user_msg(14))
        await until(lambda: len(sink.events) == 3)

    assert sink.gaps == [(12, 14)]


async def test_after_a_gap_counting_continues_from_the_received_number(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        for seq in (10, 13, 14):
            ws.push(user_msg(seq))
        await until(lambda: len(sink.events) == 3)

    assert sink.gaps == [(11, 13)]


async def test_sequence_is_per_connection_so_heartbeats_count_toward_it(rig: Rig) -> None:
    # sequence_num spans the connection, not a channel: a heartbeat between two user
    # messages consumes a number, and must not look like a gap.
    sink = Sink()
    async with await subscribe(rig, sink):
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1))
        ws.push(heartbeat_msg(2))
        ws.push(user_msg(3))
        await until(lambda: len(sink.events) == 2)

    assert sink.gaps == []


async def test_new_connection_resets_the_sequence_expectation(rig: Rig) -> None:
    sink = Sink()
    async with await subscribe(rig, sink):
        first = await rig.subscribed_socket(0)
        first.push(user_msg(500))
        first.push(user_msg(501))
        await until(lambda: len(sink.events) == 2)
        first.drop()

        second = await rig.subscribed_socket(1)
        second.push(user_msg(1))
        second.push(user_msg(2))
        await until(lambda: len(sink.events) == 4)

    assert sink.gaps == []
