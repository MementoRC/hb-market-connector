"""SubscriptionsMixin: subscribe_orderbook and subscribe_trades for IbGatewayGateway."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from market_connector.exchanges.interactive_brokers.mixins.protocols import (
        HasContractResolver,
        HasIbTransport,
    )

_log = logging.getLogger(__name__)

# IB hard-limits concurrent subscriptions; warn early at 80% of the 100-line limit.
_SUBSCRIPTION_WARN_THRESHOLD: int = 80

# Module-level counter across all instances (simple; no lock needed for CPython GIL).
_active_subscriptions: int = 0


class SubscriptionsMixin:
    """Mixin providing streaming subscription methods for IbGatewayGateway.

    Self-type contract: concrete class must satisfy HasIbTransport and HasContractResolver.
    Instance must have _tick_counter: dict initialized in __init__.
    """

    @asynccontextmanager
    async def subscribe_orderbook(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[object], None],
    ) -> AsyncGenerator[None, None]:
        """Stream DOM depth updates for trading_pair via IB reqMktDepth.

        Yields control to the caller; cleanup (handle.close()) runs on any exit.
        """
        global _active_subscriptions

        contract = await self._contract_resolver.resolve_from_pair(trading_pair)

        def _on_dom_update(ticker: object) -> None:
            try:
                # Build OrderBookUpdate from ticker.domBids / ticker.domAsks.
                bids = [
                    (Decimal(str(lv.price)), Decimal(str(lv.size)))
                    for lv in getattr(ticker, "domBids", [])
                ]
                asks = [
                    (Decimal(str(lv.price)), Decimal(str(lv.size)))
                    for lv in getattr(ticker, "domAsks", [])
                ]
                # Late import to avoid circular dep.
                from market_connector.primitives import OrderBookUpdate  # noqa: PLC0415

                callback(
                    OrderBookUpdate(trading_pair=trading_pair, bids=bids, asks=asks, update_id=0)
                )
            except Exception:
                _log.warning(
                    "Exception in subscribe_orderbook callback for %s", trading_pair, exc_info=True
                )

        handle = self._transport.subscribe("depth", contract, _on_dom_update)
        _active_subscriptions += 1
        if _active_subscriptions > _SUBSCRIPTION_WARN_THRESHOLD:
            _log.warning(
                "Active IB subscriptions (%d) exceeds threshold %d. "
                "IB enforces a ~100-line limit; consider reducing concurrent subscriptions.",
                _active_subscriptions,
                _SUBSCRIPTION_WARN_THRESHOLD,
            )
        try:
            yield
        finally:
            _active_subscriptions -= 1
            handle.close()
