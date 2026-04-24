"""Sync wrapper adapting ExchangeGateway -> strategy-framework protocols.

This is the ONLY module in hb-market-connector that imports strategy-framework.
Uses run_coroutine_threadsafe because the event loop is always running
in the hummingbot process.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decimal import Decimal

    from market_connector.protocols import ExchangeGateway


class LiveMarketAccess:
    """Adapts async ExchangeGateway to sync MarketAccessProtocol.

    Satisfies strategy_framework.protocols.market.MarketAccessProtocol
    and strategy_framework.protocols.market_data.MarketDataProtocol.

    Args:
        gateway: The async exchange gateway to wrap.
        trading_pair: Default trading pair for single-pair methods.
        loop: The running event loop (from the hummingbot process).
        timeout: Maximum seconds to wait for each async call.
    """

    DEFAULT_TIMEOUT: float = 30.0

    def __init__(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
        loop: asyncio.AbstractEventLoop,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._gateway = gateway
        self._trading_pair = trading_pair
        self._loop = loop
        self._timeout = timeout

    def _run(self, coro: Any) -> Any:
        """Submit coroutine to the running loop and block for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout)
        except FuturesTimeoutError:
            future.cancel()
            raise TimeoutError(f"Gateway call timed out after {self._timeout}s") from None

    # --- MarketAccessProtocol ---

    def place_order(self, order_type: str, side: str, amount: Decimal, price: Decimal) -> str:
        return self._run(
            self._gateway.place_order(self._trading_pair, order_type, side, amount, price)
        )

    def cancel_order(self, order_id: str) -> bool:
        return self._run(self._gateway.cancel_order(self._trading_pair, order_id))

    def get_mid_price(self) -> Decimal:
        return self._run(self._gateway.get_mid_price(self._trading_pair))

    def get_available_balance(self, currency: str) -> Decimal:
        return self._run(self._gateway.get_balance(currency))

    # --- MarketDataProtocol ---

    def get_order_book_snapshot(self, trading_pair: str | None = None) -> Any:
        pair = trading_pair or self._trading_pair
        return self._run(self._gateway.get_orderbook(pair))

    def get_candles(self, trading_pair: str, interval: str, limit: int) -> list:
        return self._run(self._gateway.get_candles(trading_pair, interval, limit))
