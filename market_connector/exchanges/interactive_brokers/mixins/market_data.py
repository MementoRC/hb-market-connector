"""MarketDataMixin: get_orderbook, get_mid_price, get_candles for IB gateway."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.mixins.protocols import (  # noqa: F401
        HasContractResolver,
        HasIbTransport,
    )

# Time to allow IB depth snapshot to populate after reqMktDepth (event-driven, not awaitable).
_DEPTH_SETTLE_SECS: float = 0.1


class MarketDataMixin:
    """Mixin providing market-data snapshot methods for IbGatewayGateway.

    Self-type contract: the concrete class must also satisfy HasIbTransport
    and HasContractResolver (verified by mypy at static analysis time).
    """

    async def get_orderbook(
        self,
        trading_pair: str,
    ) -> object:  # returns OrderBookSnapshot; typed loosely to avoid circular import
        """Snapshot DOM depth for trading_pair via reqMktDepth."""
        contract = await self._contract_resolver.resolve_from_pair(trading_pair)  # type: ignore[attr-defined]
        bids, asks = await _snapshot_depth(self._transport._ib, contract)  # type: ignore[attr-defined]

        # Late import to avoid circular dependency with framework.
        from market_connector.primitives import OrderBookSnapshot  # noqa: PLC0415

        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )


async def _snapshot_depth(
    ib: object,
    contract: object,
    *,
    num_rows: int = 10,
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    """Own the reqMktDepth lifecycle: start, settle, read, cancel."""
    ticker = ib.reqMktDepth(contract, numRows=num_rows, isSmartDepth=True)  # type: ignore[attr-defined]
    try:
        await asyncio.sleep(_DEPTH_SETTLE_SECS)
        bids = [(Decimal(str(lv.price)), Decimal(str(lv.size))) for lv in ticker.domBids]
        asks = [(Decimal(str(lv.price)), Decimal(str(lv.size))) for lv in ticker.domAsks]
        return bids, asks
    finally:
        ib.cancelMktDepth(contract, isSmartDepth=True)  # type: ignore[attr-defined]
