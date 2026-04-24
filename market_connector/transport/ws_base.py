# market_connector/transport/ws_base.py
"""Reconnecting async WebSocket client with subscription management.

Connectors compose this class. Subscriptions survive reconnects --
the client automatically re-subscribes after connection recovery.

Note: Connector mixins must wrap Subscription objects in an AsyncContextManager
to satisfy the ExchangeGateway protocol's subscribe_* return types.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from market_connector.exceptions import GatewayNotStartedError, SubscriptionLimitError

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict[str, Any]], None]


@dataclass
class Subscription:
    """Handle for an active WS subscription."""

    channel: str
    callback: MessageCallback
    active: bool = True
    _owner: WsConnectorBase | None = field(default=None, repr=False)

    async def cancel(self) -> None:
        """Cancel this subscription and free the slot."""
        if self.active and self._owner is not None:
            self._owner._remove_subscription(self)
        self.active = False


class WsConnectorBase:
    """Reconnecting WebSocket client with subscription registry.

    Args:
        url: WebSocket URL.
        auth: Async callable for auth (called on each connect).
        heartbeat_interval: Seconds between heartbeat pings.
        reconnect_delay: Initial reconnect delay in seconds.
        max_reconnect_delay: Maximum reconnect delay (exponential backoff cap).
        max_subscriptions: Maximum concurrent subscriptions (0 = unlimited).
    """

    def __init__(
        self,
        url: str,
        auth: Callable[..., Any] | None = None,
        heartbeat_interval: float = 30.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        max_subscriptions: int = 0,
    ) -> None:
        self._url = url
        self._auth = auth
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._max_subscriptions = max_subscriptions
        self._ws: Any = None
        self._connected = False
        self._subscriptions: dict[str, Subscription] = {}
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Connect to the WebSocket server and start listening."""
        import websockets

        self._ws = await websockets.connect(self._url)
        self._connected = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("WS connected to %s", self._url)

    async def disconnect(self) -> None:
        """Disconnect and cancel all subscriptions."""
        self._connected = False
        for sub in list(self._subscriptions.values()):
            sub.active = False
        self._subscriptions.clear()
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("WS disconnected from %s", self._url)

    async def subscribe(self, channel: str, callback: MessageCallback) -> Subscription:
        """Register a subscription. Raises SubscriptionLimitError if cap exceeded."""
        if not self._connected:
            raise GatewayNotStartedError("WebSocket not connected")
        if self._max_subscriptions > 0 and len(self._subscriptions) >= self._max_subscriptions:
            raise SubscriptionLimitError(f"subscription limit reached: {self._max_subscriptions}")
        sub = Subscription(channel=channel, callback=callback, _owner=self)
        self._subscriptions[channel] = sub
        return sub

    def _remove_subscription(self, sub: Subscription) -> None:
        self._subscriptions.pop(sub.channel, None)

    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the WebSocket server."""
        if not self._connected or self._ws is None:
            raise GatewayNotStartedError("WebSocket not connected")
        import json

        await self._ws.send(json.dumps(message))

    def _route_message(self, channel: str, data: dict[str, Any]) -> None:
        """Route a parsed message to the registered callback."""
        sub = self._subscriptions.get(channel)
        if sub and sub.active:
            sub.callback(data)

    async def _listen_loop(self) -> None:
        """Listen for messages and route them. Reconnects on failure."""
        import json

        delay = self._reconnect_delay
        while self._connected:
            try:
                async for raw in self._ws:
                    msg = json.loads(raw)
                    channel = msg.get("channel", msg.get("type", ""))
                    self._route_message(channel, msg)
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
