"""SubscriptionsMixin: WebSocket orderbook and trade subscriptions via Coinbase WS API.

Handler registration routes through :class:`~market_connector.transport.ws_base.WsConnectorBase`'s
``subscribe``/``unsubscribe`` pair (event-bus-backed routing table, ADR 0001
Group D, issue #44) — the same synchronous 3-arg contract used by the Kraken
subscriptions mixin.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.converters import (
    to_exchange_pair,
    to_orderbook_update,
    to_trade_event,
)
from market_connector.exchanges.coinbase.schemas.ws import Level2Event, MarketTradesEvent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from market_connector.exchanges.coinbase.mixins.protocols import HasReady, HasRest, HasWs
    from market_connector.primitives import OrderBookUpdate, TradeEvent


class SubscriptionsMixin:
    async def subscribe_orderbook(
        self: HasWs & HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[OrderBookUpdate], None],
    ) -> Any:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        product_id = to_exchange_pair(trading_pair)
        channel = "level2"
        update_id_counter = [0]

        def _dispatch(msg: dict[str, Any]) -> None:
            for evt in msg.get("events", []):
                if evt.get("product_id") != product_id:
                    continue
                level2_evt = Level2Event.model_validate(evt)
                update_id_counter[0] += 1
                callback(to_orderbook_update(level2_evt, update_id=update_id_counter[0]))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, product_id, _dispatch)
            try:
                yield
            finally:
                self._ws.unsubscribe(channel, product_id)

        return _ctx()

    async def subscribe_trades(
        self: HasWs & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[TradeEvent], None],
    ) -> Any:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        product_id = to_exchange_pair(trading_pair)
        channel = "market_trades"

        def _dispatch(msg: dict[str, Any]) -> None:
            for evt in msg.get("events", []):
                mte = MarketTradesEvent.model_validate(evt)
                for trade in mte.trades:
                    if trade.product_id != product_id:
                        continue
                    callback(to_trade_event(trade))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            self._ws.subscribe(channel, product_id, _dispatch)
            try:
                yield
            finally:
                self._ws.unsubscribe(channel, product_id)

        return _ctx()
