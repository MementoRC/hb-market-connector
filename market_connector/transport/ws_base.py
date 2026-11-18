# market_connector/transport/ws_base.py
"""Reconnecting async WebSocket client with WsAuthModel + WsShapeDecoder integration.

Connectors compose this class. The auth model handles all authentication lifecycle
(URL rewriting, login frames, per-message signing, token refresh). The decoder
classifies incoming frames and extracts routing keys.

Handler registration via subscribe(channel, pair, handler) is a routing-table
operation only — wire-level subscribe messages are the exchange connector's
responsibility.

Management messages (SUBSCRIBE_ACK, ERROR) land on the public asyncio.Queue
``management_messages``. Heartbeat and UNKNOWN frames are silently absorbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import websockets

from market_connector.exceptions import GatewayNotStartedError
from market_connector.hb_compat.event_bus import WsRoutingTable
from market_connector.hb_compat.logging import get_logger
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind, WsShapeDecoder

if TYPE_CHECKING:
    from market_connector.ws_models.auth_models import WsAuthModel

logger = get_logger(__name__)

MessageCallback = Callable[[Any], None]


class WsConnectorBase:
    """Reconnecting WebSocket client with WsAuthModel and WsShapeDecoder integration.

    Args:
        url: Base WebSocket URL (may be rewritten by ws_auth.prepare_connection).
        ws_auth: WsAuthModel instance handling all auth lifecycle hooks.
        decoder: WsShapeDecoder instance for stage-1 frame classification.
        heartbeat_interval: Seconds between heartbeat pings.
        reconnect_delay: Initial reconnect delay in seconds.
        max_reconnect_delay: Maximum reconnect delay (exponential backoff cap).
        max_subscriptions: Maximum concurrent handler registrations (0 = unlimited).
        refresh_interval: Seconds between ws_auth.refresh() calls. None disables.
    """

    def __init__(
        self,
        url: str,
        ws_auth: WsAuthModel,
        decoder: WsShapeDecoder,
        heartbeat_interval: float = 30.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        max_subscriptions: int = 0,
        refresh_interval: float | None = None,
    ) -> None:
        self._url = url
        self._ws_auth = ws_auth
        self._decoder = decoder
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._max_subscriptions = max_subscriptions
        self._refresh_interval = refresh_interval
        self._ws: Any = None
        self._connected = False
        self._closed = False  # set by disconnect(): the reconnect loop must stop for good
        self._generation = 0  # successful connects so far
        self._lifecycle_lock = asyncio.Lock()  # serializes connect() / disconnect()
        self._dropped = asyncio.Event()  # set when the listen loop sees a drop; cleared on connect
        # Routing table: (channel, pair | None) -> handler, backed by hb-event-bus
        # (ADR 0001 Group D, issue #44).
        self._routing_table: WsRoutingTable = WsRoutingTable(name=f"ws-base:{url}")
        self._supervisor_task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        # Management messages: SUBSCRIBE_ACK and ERROR frames land here
        self.management_messages: asyncio.Queue[NormalizedWsMessage] = asyncio.Queue()

    @property
    def is_connected(self) -> bool:
        """True while a socket is open; False from drop detection until the reconnect succeeds."""
        return self._connected and self._ws is not None

    @property
    def connection_generation(self) -> int:
        """Number of successful connects so far; increments on every (re)connect."""
        return self._generation

    async def connect(self) -> None:
        """Connect to the WebSocket server and start the single supervisor task.

        The supervisor owns the socket afterwards and reconnects in place (one listen,
        heartbeat and refresh task per live socket); calling connect() while it runs is a no-op.
        """
        async with self._lifecycle_lock:
            if self._supervisor_task is not None and not self._supervisor_task.done():
                return
            self._closed = False
            await self._open()
            self._supervisor_task = asyncio.create_task(self._supervise())

    async def _open(self) -> None:
        """Open one socket: prepare URL, dial, run auth hook, (re)start the per-socket tasks."""
        url = await self._ws_auth.prepare_connection(self._url)
        self._ws = await websockets.connect(url)
        try:
            await self._ws_auth.on_connected(self._raw_send)
        except BaseException:
            await self._discard_socket()
            raise
        self._connected = True  # only once the auth hook has succeeded
        self._generation += 1
        self._dropped.clear()
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._restart_keepalive_tasks()
        logger.info("WS connected to %s", url)

    async def _discard_socket(self) -> None:
        self._connected = False
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _restart_keepalive_tasks(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self._refresh_interval is not None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _stop_keepalive_tasks(self) -> None:
        """Cancel the heartbeat/refresh tasks and wait until they are gone.

        An outer cancellation of the awaiting task still propagates.
        """
        tasks = [t for t in (self._heartbeat_task, self._refresh_task) if t is not None]
        self._heartbeat_task = None
        self._refresh_task = None
        pending = [t for t in tasks if not t.done()]
        for task in pending:
            task.cancel()
        others = [t for t in pending if t is not asyncio.current_task()]
        await asyncio.gather(*others, return_exceptions=True)

    async def wait_disconnect(self, generation: int | None = None) -> None:
        """Return once the given connection generation has dropped.

        ``None`` means the current connection. A generation older than the current one has
        already dropped (returns immediately), as does any wait after ``disconnect()``.
        """
        if generation is not None and generation < self._generation:
            return
        await self._dropped.wait()

    async def disconnect(self) -> None:
        """Disconnect, permanently stop the reconnect loop and cancel all background tasks."""
        async with self._lifecycle_lock:
            self._closed = True
            self._connected = False
            self._dropped.set()  # wake wait_disconnect() waiters
            self._routing_table.clear()
            tasks = [self._supervisor_task, self._listen_task]
            pending = [t for t in tasks if t is not None and not t.done()]
            for task in pending:
                task.cancel()
            others = [t for t in pending if t is not asyncio.current_task()]
            await asyncio.gather(*others, return_exceptions=True)
            await self._stop_keepalive_tasks()
            await self._discard_socket()
            logger.info("WS disconnected from %s", self._url)

    def subscribe(self, channel: str, pair: str | None, handler: MessageCallback) -> None:
        """Register a handler for (channel, pair) routing key.

        This is a routing-table registration only. The caller is responsible
        for sending any wire-level subscribe message via send().

        Args:
            channel: Channel name to match against decoded msg.channel.
            pair: Trading pair to match, or None for channel-wide handler.
            handler: Callable invoked with msg.payload on DATA frames.
        """
        if self._max_subscriptions > 0 and len(self._routing_table) >= self._max_subscriptions:
            from market_connector.exceptions import SubscriptionLimitError

            raise SubscriptionLimitError(f"subscription limit reached: {self._max_subscriptions}")
        self._routing_table.subscribe(channel, pair, handler)

    def unsubscribe(self, channel: str, pair: str | None) -> None:
        """Remove the handler registered for ``(channel, pair)``. Idempotent.

        Public counterpart to :meth:`subscribe`; callers (e.g. exchange
        subscription mixins) should use this instead of reaching into the
        routing table directly.
        """
        self._routing_table.unsubscribe(channel, pair)

    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the WebSocket server.

        Calls ws_auth.transform_outgoing(message) before raw send, allowing
        the auth model to inject signatures or tokens.
        """
        if not self._connected or self._ws is None:
            raise GatewayNotStartedError("WebSocket not connected")
        transformed = await self._ws_auth.transform_outgoing(message)
        await self._raw_send(transformed)

    async def _raw_send(self, message: dict[str, Any]) -> None:
        """Send a raw dict as JSON without auth transformation.

        Used exclusively by ws_auth.on_connected to send login frames.
        Must NOT call transform_outgoing (would create an infinite loop for
        PerMessageSignAuth and would double-sign login frames).
        """
        if self._ws is None:
            raise GatewayNotStartedError("WebSocket not connected")
        await self._ws.send(json.dumps(message))

    def _route_message(self, msg: NormalizedWsMessage) -> None:
        """Route a decoded NormalizedWsMessage to the appropriate handler."""
        if msg.kind == WsMessageKind.DATA:
            channel = msg.channel or ""
            routed = self._routing_table.route(channel, msg.pair, msg.payload)
            if not routed:
                logger.debug("No handler for (%s, %s) — frame dropped", msg.channel, msg.pair)
        elif msg.kind == WsMessageKind.HEARTBEAT:
            pass  # silently absorbed
        elif msg.kind in (WsMessageKind.SUBSCRIBE_ACK, WsMessageKind.ERROR):
            self.management_messages.put_nowait(msg)
        else:
            logger.debug("UNKNOWN frame — dropped: %r", msg)

    async def _listen_loop(self) -> None:
        """Read the current socket until it ends or fails (both are a drop), routing frames."""
        # a failed connection is handled like a clean close by the supervisor
        with contextlib.suppress(Exception):
            async for raw in self._ws:
                try:
                    parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    self._route_message(self._decoder.decode(parsed))
                except Exception:
                    logger.exception("Error processing WS frame")

    async def _supervise(self) -> None:
        """Own the socket: wait for each listen task to end (a drop), then reconnect in place."""
        while not self._closed:
            listener = self._listen_task
            if listener is not None:
                await asyncio.wait({listener})
            if self._closed:
                break
            self._connected = False  # is_connected is False from drop detection on
            self._dropped.set()
            await self._stop_keepalive_tasks()
            await self._discard_socket()  # best-effort close of the dead socket
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Retry with exponential backoff until a connection is open (or disconnect())."""
        delay = self._reconnect_delay
        while not self._closed:
            logger.warning("WS connection lost, reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)
            try:
                await self._open()
            except Exception:
                logger.exception("WS reconnect failed")
            else:
                return

    async def _heartbeat_loop(self) -> None:
        """Send periodic pings to keep the connection alive."""
        while self._connected:
            await asyncio.sleep(self._heartbeat_interval)
            if self._ws and self._connected:
                with contextlib.suppress(Exception):
                    await self._ws.ping()

    async def _refresh_loop(self) -> None:
        """Call ws_auth.refresh() at configured interval."""
        while self._connected:
            await asyncio.sleep(self._refresh_interval)  # type: ignore[arg-type]
            if self._connected:
                try:
                    await self._ws_auth.refresh()
                except Exception:
                    logger.exception("WS auth refresh failed")
