"""Review hardening for the Kraken private WS (#274): token placement, ack-gate lifecycle, rollback.

Reuses the #274 rig (real gateway, fake sockets, a REST double that only has ``request``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exceptions import (
    GatewayNotStartedError,
    SubscriptionAckTimeoutError,
    SubscriptionRejectedError,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.mixins.subscriptions import SubscriptionsMixin
from market_connector.exchanges.kraken.specs import KRAKEN_WS_DECODER
from market_connector.ws_models.decoder import WsMessageKind
from tests.exchanges.kraken.unit.test_private_ws_274 import (
    TokenRest,
    enter,
    leave,
    open_rig,
    reject,
    silent,
    subscription_ack,
    until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.exchanges.kraken.unit.test_private_ws_274 import Rig

_ACK_TIMEOUT = 0.2


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    async with open_rig(monkeypatch, ack_timeout=_ACK_TIMEOUT) as opened:
        yield opened


# ---------------------------------------------------------------------------
# H1: the token is nested under "subscription" (Kraken WS v1), never top-level
# ---------------------------------------------------------------------------


class TestNestedToken:
    @pytest.mark.parametrize(
        ("method", "name"),
        [("subscribe_own_trades", "ownTrades"), ("subscribe_open_orders", "openOrders")],
    )
    async def test_subscribe_and_unsubscribe_frames_nest_the_token(
        self, rig: Rig, method: str, name: str
    ) -> None:
        ctx = await enter(getattr(rig.gw, method))
        await leave(ctx)
        frames = rig.priv.frames("subscribe") + rig.priv.frames("unsubscribe")
        assert len(frames) == 2
        for frame in frames:
            assert frame["subscription"]["token"] == rig.rest.token
            assert "token" not in frame
            assert frame["subscription"]["name"] == name


# ---------------------------------------------------------------------------
# H2: no silent AckGate fallback
# ---------------------------------------------------------------------------


class _Ws:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def subscribe(self, channel: str, pair: str | None, handler: Any) -> None:
        return None

    def unsubscribe(self, channel: str, pair: str | None) -> None:
        return None

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class _GatelessHost(SubscriptionsMixin):
    ready = True

    def __init__(self) -> None:
        self._ws = self._ws_private = _Ws()
        self._config = SimpleNamespace(subscribe_ack_timeout=_ACK_TIMEOUT)


class TestMissingAckGate:
    async def test_host_without_ack_gate_fails_loudly_not_by_timeout(self) -> None:
        host = _GatelessHost()
        ctx = await host.subscribe_own_trades(print)
        with pytest.raises(RuntimeError, match="_ack_gate"):
            await asyncio.wait_for(ctx.__aenter__(), 1.0)
        assert host._ws_private.sent == []


# ---------------------------------------------------------------------------
# H3: drain consumer survives bad messages; stop() cleans up regardless
# ---------------------------------------------------------------------------


def _spy_teardown(gw: KrakenGateway, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for label, target, name in (
        ("priv", gw._ws_private, "disconnect"),
        ("pub", gw._ws, "disconnect"),
        ("rest", gw._rest, "close"),
    ):
        original = getattr(target, name)

        async def _wrapped(label: str = label, original: Any = original) -> None:
            calls.append(label)
            await original()

        monkeypatch.setattr(target, name, _wrapped)
    return calls


class TestDrainAndStop:
    async def test_malformed_ack_does_not_kill_the_drain(self, rig: Rig) -> None:
        def junk_then_ack(frame: dict[str, Any]) -> dict[str, Any]:
            rig.conn.management_messages.put_nowait(object())  # type: ignore[arg-type]
            return subscription_ack(frame)

        rig.responder = junk_then_ack
        ctx = await asyncio.wait_for(enter(rig.gw.subscribe_own_trades), 1.0)
        await leave(ctx)

    async def test_stop_cleans_up_when_the_ack_task_died(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = rig.gw._ack_task
        assert live is not None
        live.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await live

        async def _boom() -> None:
            raise RuntimeError("drain died")

        dead = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        rig.gw._ack_task = dead
        calls = _spy_teardown(rig.gw, monkeypatch)
        await rig.gw.stop()
        assert calls == ["priv", "pub", "rest"]

    async def test_outer_cancellation_of_stop_is_not_swallowed(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = rig.gw._ack_task
        assert live is not None
        live.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await live

        async def _slow_to_cancel() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise

        slow = asyncio.create_task(_slow_to_cancel())
        await asyncio.sleep(0)
        rig.gw._ack_task = slow
        calls = _spy_teardown(rig.gw, monkeypatch)
        stopping = asyncio.create_task(rig.gw.stop())
        await asyncio.sleep(0.05)
        stopping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopping
        assert stopping.cancelled()
        assert calls == ["priv", "pub", "rest"]  # cleanup still ran
        await asyncio.gather(slow, return_exceptions=True)


# ---------------------------------------------------------------------------
# H4: pending acks fail promptly on stop()
# ---------------------------------------------------------------------------


class TestStopFailsPendingAcks:
    async def test_pending_subscribe_fails_fast_on_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with open_rig(monkeypatch, ack_timeout=5.0) as rig:
            rig.responder = silent
            pending = asyncio.create_task(enter(rig.gw.subscribe_own_trades))
            await until(lambda: bool(rig.priv.frames("subscribe")))
            started = time.monotonic()
            await rig.gw.stop()
            with pytest.raises(GatewayNotStartedError):
                await asyncio.wait_for(pending, 1.0)
            assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# H5: best-effort unsubscribe after an unacked subscribe
# ---------------------------------------------------------------------------


class TestUnsubscribeAfterUnackedSubscribe:
    async def test_timeout_sends_unsubscribe(self, rig: Rig) -> None:
        rig.responder = silent
        with pytest.raises(SubscriptionAckTimeoutError):
            await enter(rig.gw.subscribe_own_trades)
        (frame,) = rig.priv.frames("unsubscribe")
        assert frame["subscription"]["name"] == "ownTrades"

    async def test_cancellation_sends_unsubscribe(self, rig: Rig) -> None:
        rig.responder = silent
        pending = asyncio.create_task(enter(rig.gw.subscribe_open_orders))
        await until(lambda: bool(rig.priv.frames("subscribe")))
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await until(lambda: bool(rig.priv.frames("unsubscribe")))
        assert rig.priv.frames("unsubscribe")[0]["subscription"]["name"] == "openOrders"

    async def test_rejection_needs_no_unsubscribe(self, rig: Rig) -> None:
        rig.responder = reject
        with pytest.raises(SubscriptionRejectedError):
            await enter(rig.gw.subscribe_own_trades)
        assert rig.priv.frames("unsubscribe") == []

    async def test_unsubscribe_send_errors_do_not_mask_the_timeout(self, rig: Rig) -> None:
        rig.responder = silent
        original = rig.priv.send

        async def _fail_unsub(data: str) -> None:
            if json.loads(data).get("event") == "unsubscribe":
                raise ConnectionError("gone")
            await original(data)

        rig.priv.send = _fail_unsub  # type: ignore[method-assign]
        with pytest.raises(SubscriptionAckTimeoutError):
            await enter(rig.gw.subscribe_own_trades)


# ---------------------------------------------------------------------------
# H6: start() rollback is complete
# ---------------------------------------------------------------------------


def _lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    public_fails: bool = False,
    private_fails: bool = False,
    private_disconnect_fails: bool = False,
) -> tuple[KrakenGateway, TokenRest, list[str]]:
    log: list[str] = []
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    rest = TokenRest(log=log)
    gw._rest = rest  # type: ignore[assignment]
    state = {"public_fails": public_fails, "private_fails": private_fails}
    for attr, label in (("_ws", "ws"), ("_ws_private", "priv")):
        conn = getattr(gw, attr)

        async def _connect(label: str = label) -> None:
            log.append(f"{label}:connect")
            if state["public_fails"] and label == "ws":
                raise ConnectionError("public connect failed")
            if state["private_fails"] and label == "priv":
                raise ConnectionError("private connect failed")

        async def _disconnect(label: str = label) -> None:
            log.append(f"{label}:disconnect")
            if private_disconnect_fails and label == "priv":
                raise RuntimeError("disconnect blew up")

        monkeypatch.setattr(conn, "connect", _connect)
        monkeypatch.setattr(conn, "disconnect", _disconnect)
    gw._test_state = state  # type: ignore[attr-defined]
    return gw, rest, log


class TestStartRollback:
    async def test_public_connect_failure_closes_rest_and_resets_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gw, rest, _ = _lifecycle(monkeypatch, public_fails=True)
        with pytest.raises(ConnectionError, match="public connect failed"):
            await gw.start()
        assert rest.closed
        assert (gw._opened, gw._started, gw._ack_task) == (False, False, None)

    async def test_private_connect_failure_rolls_everything_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gw, rest, log = _lifecycle(monkeypatch, private_fails=True)
        with pytest.raises(ConnectionError, match="private connect failed"):
            await gw.start()
        assert "ws:disconnect" in log
        assert "priv:disconnect" in log
        assert rest.closed
        assert (gw._opened, gw._started, gw._ack_task) == (False, False, None)

    async def test_cleanup_errors_never_mask_the_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gw, rest, log = _lifecycle(monkeypatch, private_fails=True, private_disconnect_fails=True)
        with pytest.raises(ConnectionError, match="private connect failed"):
            await gw.start()
        assert "ws:disconnect" in log
        assert rest.closed

    async def test_start_works_again_after_a_rollback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gw, rest, _ = _lifecycle(monkeypatch, private_fails=True)
        with pytest.raises(ConnectionError):
            await gw.start()
        gw._test_state["private_fails"] = False  # type: ignore[attr-defined]
        await gw.start()
        try:
            assert gw.ready
            assert gw._ack_task is not None
        finally:
            await gw.stop()
        assert rest.closed


# ---------------------------------------------------------------------------
# H7: the private-frame heuristic only claims ownTrades / openOrders
# ---------------------------------------------------------------------------


class TestPrivateFrameHeuristic:
    def test_foreign_three_element_frame_is_not_a_private_data_frame(self) -> None:
        raw = [{"a": 1}, "someOtherChannel", {}]
        try:
            msg = KRAKEN_WS_DECODER.decode(raw)
        except Exception:  # noqa: BLE001 - falling through to the stock decoder may reject it
            return
        private_shape = msg.kind is WsMessageKind.DATA and msg.pair is None and msg.payload == raw
        assert not private_shape

    @pytest.mark.parametrize("channel", ["ownTrades", "openOrders"])
    def test_private_channels_still_decode(self, channel: str) -> None:
        raw = [[{"x": {}}], channel, {"sequence": 1}]
        msg = KRAKEN_WS_DECODER.decode(raw)
        assert (msg.kind, msg.channel, msg.payload) == (WsMessageKind.DATA, channel, raw)


# ---------------------------------------------------------------------------
# H8: the public connector's management queue is drained too
# ---------------------------------------------------------------------------


class TestPublicQueueDrained:
    async def test_public_acks_do_not_pile_up(self, rig: Rig) -> None:
        public = rig.gw._ws.management_messages
        for i in range(20):
            rig.pub.push(
                {
                    "event": "subscriptionStatus",
                    "status": "subscribed",
                    "pair": "XBT/USD",
                    "channelName": "trade",
                    "subscription": {"name": "trade"},
                    "channelID": i,
                }
            )
        await until(lambda: rig.pub._queue.empty())  # the listen loop took every frame
        await asyncio.sleep(0.05)
        assert public.qsize() == 0

    async def test_public_error_acks_are_logged_as_warnings(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            rig.pub.push(
                {
                    "event": "subscriptionStatus",
                    "status": "error",
                    "errorMessage": "Currency pair not supported",
                    "pair": "FOO/BAR",
                    "subscription": {"name": "trade"},
                }
            )
            await until(lambda: "Currency pair not supported" in caplog.text)
