"""SubscriptionsMixin: WebSocket channel subscriptions via Kraken WS API v1."""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.schemas.ws import (
    BookSnapshot,
    OpenOrdersEvent,
    OwnTradesEvent,
    TradeEvent,
)
from market_connector.exchanges.kraken.specs import KRAKEN_WS_DECODER
from market_connector.ws_models.decoder import WsMessageKind

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasWs


class SubscriptionsMixin:
    """Mixin providing WebSocket channel subscription helpers for Kraken.

    Public channels (book, trade) use PassThroughAuth — no token needed.
    Private channels (ownTrades, openOrders) use TokenFetchAuth — the token
    is injected automatically by the auth model at send time.

    Each ``subscribe_*`` method returns an async context manager.  Entering it
    registers the handler and sends the subscribe wire message; exiting removes
    the handler and sends an unsubscribe message (best-effort).

    Handlers receive the raw frame so that schemas such as
    :class:`~..schemas.ws.OwnTradesEvent` can reconstruct from the full array
    (Kraken v1 WS quirk: private-channel payload at index 1 is the channel
    name string, not the data).

    Example::

        async with await connector.subscribe_orderbook("XBT/USD", on_book):
            await asyncio.sleep(60)
    """

    async def subscribe_orderbook(
        self: HasWs & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[BookSnapshot], None],
        depth: int = 25,
    ) -> Any:
        """Subscribe to the ``book-<depth>`` channel for *trading_pair*.

        Args:
            trading_pair: Kraken WS pair name (e.g. ``"XBT/USD"``).
            callback: Invoked with a :class:`~..schemas.ws.BookSnapshot` on
                each snapshot frame.
            depth: Order-book depth requested (default ``25``).

        Returns:
            Async context manager; yields ``None``.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        channel = f"book-{depth}"

        def _handler(raw: Any) -> None:
            normalized = KRAKEN_WS_DECODER.decode(raw)
            if normalized.kind != WsMessageKind.DATA:
                return
            callback(BookSnapshot.from_payload(normalized.payload))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, trading_pair, _handler)
            await self._ws.send(
                {
                    "event": "subscribe",
                    "pair": [trading_pair],
                    "subscription": {"name": "book", "depth": depth},
                }
            )
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    await self._ws.send(
                        {
                            "event": "unsubscribe",
                            "pair": [trading_pair],
                            "subscription": {"name": "book", "depth": depth},
                        }
                    )
                self._ws._handlers.pop((channel, trading_pair), None)

        return _ctx()

    async def subscribe_trades(
        self: HasWs & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[TradeEvent], None],
    ) -> Any:
        """Subscribe to the ``trade`` channel for *trading_pair*.

        Args:
            trading_pair: Kraken WS pair name (e.g. ``"XBT/USD"``).
            callback: Invoked with a :class:`~..schemas.ws.TradeEvent` on
                each trade frame.

        Returns:
            Async context manager; yields ``None``.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        channel = "trade"

        def _handler(raw: Any) -> None:
            normalized = KRAKEN_WS_DECODER.decode(raw)
            if normalized.kind != WsMessageKind.DATA:
                return
            callback(TradeEvent.from_frame(raw))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, trading_pair, _handler)
            await self._ws.send(
                {
                    "event": "subscribe",
                    "pair": [trading_pair],
                    "subscription": {"name": "trade"},
                }
            )
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    await self._ws.send(
                        {
                            "event": "unsubscribe",
                            "pair": [trading_pair],
                            "subscription": {"name": "trade"},
                        }
                    )
                self._ws._handlers.pop((channel, trading_pair), None)

        return _ctx()

    async def subscribe_own_trades(
        self: HasWs & HasReady,  # type: ignore[valid-type]
        callback: Callable[[OwnTradesEvent], None],
    ) -> Any:
        """Subscribe to the private ``ownTrades`` channel.

        The WS token is injected by ``TokenFetchAuth`` at send time.

        Kraken v1 WS quirk: the ownTrades frame is
        ``[trades_list, "ownTrades", {"sequence": N}]``.
        The raw frame is passed to :meth:`~..schemas.ws.OwnTradesEvent.from_raw_frame`
        which reconstructs the full event from the array.

        Args:
            callback: Invoked with an :class:`~..schemas.ws.OwnTradesEvent`
                for each ownTrades frame.

        Returns:
            Async context manager; yields ``None``.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        channel = "ownTrades"

        def _handler(raw: Any) -> None:
            normalized = KRAKEN_WS_DECODER.decode(raw)
            if normalized.kind != WsMessageKind.DATA:
                return
            callback(OwnTradesEvent.from_raw_frame(raw))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, None, _handler)
            await self._ws.send(
                {
                    "event": "subscribe",
                    "subscription": {"name": "ownTrades"},
                }
            )
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    await self._ws.send(
                        {
                            "event": "unsubscribe",
                            "subscription": {"name": "ownTrades"},
                        }
                    )
                self._ws._handlers.pop((channel, None), None)

        return _ctx()

    async def subscribe_open_orders(
        self: HasWs & HasReady,  # type: ignore[valid-type]
        callback: Callable[[OpenOrdersEvent], None],
    ) -> Any:
        """Subscribe to the private ``openOrders`` channel.

        The WS token is injected by ``TokenFetchAuth`` at send time.

        Kraken v1 WS quirk: the openOrders frame is
        ``[orders_list, "openOrders", {"sequence": N}]``.
        The raw frame is passed to :meth:`~..schemas.ws.OpenOrdersEvent.from_raw_frame`
        which reconstructs the full event from the array.

        Args:
            callback: Invoked with an :class:`~..schemas.ws.OpenOrdersEvent`
                for each openOrders frame.

        Returns:
            Async context manager; yields ``None``.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        channel = "openOrders"

        def _handler(raw: Any) -> None:
            normalized = KRAKEN_WS_DECODER.decode(raw)
            if normalized.kind != WsMessageKind.DATA:
                return
            callback(OpenOrdersEvent.from_raw_frame(raw))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, None, _handler)
            await self._ws.send(
                {
                    "event": "subscribe",
                    "subscription": {"name": "openOrders"},
                }
            )
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    await self._ws.send(
                        {
                            "event": "unsubscribe",
                            "subscription": {"name": "openOrders"},
                        }
                    )
                self._ws._handlers.pop((channel, None), None)

        return _ctx()
