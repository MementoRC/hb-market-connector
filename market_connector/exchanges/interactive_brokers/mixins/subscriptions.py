"""SubscriptionsMixin: subscribe_orderbook and subscribe_trades for IbGatewayGateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.hb_compat.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from market_connector.exchanges.interactive_brokers.mixins.protocols import (
        HasContractResolver,
        HasIbTransport,
    )

_log = get_logger(__name__)

# IB hard-limits concurrent subscriptions; warn early at 80% of the 100-line limit.
_SUBSCRIPTION_WARN_THRESHOLD: int = 80

# Module-level counter across all instances (simple; no lock needed for CPython GIL).
_active_subscriptions: int = 0

# Module-level state for synthetic trade ID generation.
# Maps (conId, second_epoch) -> counter for unique trade IDs.
_TRADE_ID_COUNTERS: dict[tuple[int, int], int] = {}


def _synthetic_trade_id(
    tick_counter: dict[tuple[int, int], int],
    con_id: int,
    tick_ns: int,
) -> str:
    """Generate f"{con_id}:{tick_ns}:{counter}" with per-(conId, second) counter.

    Counter increments when two ticks share the same nanosecond timestamp.
    Resets (key removed) when the second changes.
    """
    second = tick_ns // 1_000_000_000
    key = (con_id, second)
    count = tick_counter.get(key, 0)
    tick_counter[key] = count + 1
    # Remove stale keys from previous seconds to bound dict growth.
    stale = [k for k in tick_counter if k[1] < second]
    for k in stale:
        del tick_counter[k]
    return f"{con_id}:{tick_ns}:{count}"


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

    @asynccontextmanager
    async def subscribe_trades(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: str,
        callback: Callable[[object], None],
    ) -> AsyncGenerator[None, None]:
        """Stream trade tick updates for trading_pair via IB reqTickByTickData.

        Yields control to the caller; cleanup (handle.close()) runs on any exit.
        """
        global _active_subscriptions

        contract = await self._contract_resolver.resolve_from_pair(trading_pair)

        def _on_tick(tick: object) -> None:
            try:
                # ib_async TickByTickAllLast: time (datetime), price, size, exchange, conditions
                tick_ns = int(tick.time.timestamp() * 1e9)  # type: ignore[attr-defined]
                trade_id = _synthetic_trade_id(self._tick_counter, contract.conId, tick_ns)
                # Late import to avoid circular dep.
                from market_connector.primitives import TradeEvent, TradeType  # noqa: PLC0415

                # IB's AllLast ticks don't expose side; default BUY (Stage 3 limitation).
                callback(
                    TradeEvent(
                        trading_pair=trading_pair,
                        exchange_trade_id=trade_id,
                        price=Decimal(str(tick.price)),  # type: ignore[attr-defined]
                        amount=Decimal(str(tick.size)),  # type: ignore[attr-defined]
                        side=TradeType.BUY,
                        timestamp=tick.time.timestamp(),  # type: ignore[attr-defined]
                    )
                )
            except Exception:
                _log.warning(
                    "Exception in subscribe_trades callback for %s", trading_pair, exc_info=True
                )

        handle = self._transport.subscribe("trades", contract, _on_tick)
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
