"""Runtime-checkable gateway protocols for the exchange gateway framework.

Connectors implement ExchangeGateway. Consumers depend on the protocol,
never on a concrete connector class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager as AsyncContextManager
    from decimal import Decimal

    from market_connector.primitives import (
        OpenOrder,
        OrderBookSnapshot,
        OrderBookUpdate,
        TradeEvent,
    )

# Note: @runtime_checkable isinstance checks verify method *names* only,
# not parameter types or return types. Use mypy for full type checking.
# CandleData is intentionally not imported here — get_candles returns list
# and each connector defines its own candle type. The hb_compat bridge
# handles conversion to strategy-framework's CandleData.


@runtime_checkable
class ExecutionGateway(Protocol):
    """Protocol for order execution operations."""

    async def place_order(
        self,
        trading_pair: str,
        order_type: str,
        side: str,
        amount: Decimal,
        price: Decimal | None,
    ) -> str:
        pass

    async def cancel_order(self, trading_pair: str, client_order_id: str) -> bool:
        pass

    async def get_open_orders(self, trading_pair: str) -> list[OpenOrder]:
        pass

    async def get_balance(self, currency: str) -> Decimal:
        pass


@runtime_checkable
class MarketDataGateway(Protocol):
    """Protocol for market data operations."""

    async def get_orderbook(self, trading_pair: str) -> OrderBookSnapshot:
        pass

    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list:
        pass

    async def get_mid_price(self, trading_pair: str) -> Decimal:
        pass

    async def subscribe_orderbook(
        self,
        trading_pair: str,
        callback: Callable[[OrderBookUpdate], None],
    ) -> AsyncContextManager:
        pass

    async def subscribe_trades(
        self,
        trading_pair: str,
        callback: Callable[[TradeEvent], None],
    ) -> AsyncContextManager:
        pass


@runtime_checkable
class ExchangeGateway(ExecutionGateway, MarketDataGateway, Protocol):
    """Composite protocol: execution + market data + lifecycle."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    @property
    def ready(self) -> bool:
        return False
