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
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import websockets

from market_connector.exceptions import GatewayNotStartedError
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind, WsShapeDecoder

if TYPE_CHECKING:
    from market_connector.ws_models.auth_models import WsAuthModel

logger = logging.getLogger(__name__)

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
        # Routing table: (channel, pair | None) -> handler
        self._handlers: dict[tuple[str, str | None], MessageCallback] = {}
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        # Management messages: SUBSCRIBE_ACK and ERROR frames land here
        self.management_messages: asyncio.Queue[NormalizedWsMessage] = asyncio.Queue()

    async def connect(self) -> None:
        """Connect to the WebSocket server and start listening.

        Lifecycle:
        1. ws_auth.prepare_connection(base_url) → may rewrite URL
        2. websockets.connect(rewritten_url) → open socket
        3. ws_auth.on_connected(_raw_send) → auth model may send login frame
        4. Start listen, heartbeat, and optional refresh background tasks
        """
        url = await self._ws_auth.prepare_connection(self._url)
        self._ws = await websockets.connect(url)
        self._connected = True
        await self._ws_auth.on_connected(self._raw_send)
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self._refresh_interval is not None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("WS connected to %s", url)

    async def disconnect(self) -> None:
        """Disconnect and cancel all background tasks."""
        self._connected = False
        self._handlers.clear()
        for task in (self._listen_task, self._heartbeat_task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
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
        if self._max_subscriptions > 0 and len(self._handlers) >= self._max_subscriptions:
            from market_connector.exceptions import SubscriptionLimitError

            raise SubscriptionLimitError(f"subscription limit reached: {self._max_subscriptions}")
        self._handlers[(channel, pair)] = handler

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
            handler = self._handlers.get((msg.channel, msg.pair))
            if handler is None and msg.pair is not None:
                # Fall back to channel-wide handler
                handler = self._handlers.get((msg.channel, None))
            if handler is not None:
                handler(msg.payload)
            else:
                logger.debug("No handler for (%s, %s) — frame dropped", msg.channel, msg.pair)
        elif msg.kind == WsMessageKind.HEARTBEAT:
            pass  # silently absorbed
        elif msg.kind in (WsMessageKind.SUBSCRIBE_ACK, WsMessageKind.ERROR):
            self.management_messages.put_nowait(msg)
        else:
            logger.debug("UNKNOWN frame — dropped: %r", msg)

    async def _listen_loop(self) -> None:
        """Listen for messages, decode, and route. Reconnects on failure."""
        delay = self._reconnect_delay
        while self._connected:
            try:
                async for raw in self._ws:
                    try:
                        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        msg = self._decoder.decode(parsed)
                        self._route_message(msg)
                    except Exception:
                        logger.exception("Error processing WS frame")
            except Exception:
                if not self._connected:
                    break
                logger.warning("WS connection lost, reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                try:
                    await self.connect()
                    delay = self._reconnect_delay
                except Exception:
                    logger.exception("WS reconnect failed")

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
