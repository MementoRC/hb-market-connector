"""Test that protocols are runtime-checkable and enforce structural subtyping."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.primitives import OrderBookSnapshot
from market_connector.protocols import (
    ExchangeGateway,
    ExecutionGateway,
    MarketDataGateway,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _MockExecution:
    async def place_order(self, trading_pair, order_type, side, amount, price):
        return "order-1"

    async def cancel_order(self, trading_pair, client_order_id):
        return True

    async def get_open_orders(self, trading_pair):
        return []

    async def get_balance(self, currency):
        return Decimal("100")


class _MockMarketData:
    async def get_orderbook(self, trading_pair):
        return OrderBookSnapshot(trading_pair=trading_pair, bids=[], asks=[], timestamp=0.0)

    async def get_candles(self, trading_pair, interval, limit):
        return []

    async def get_mid_price(self, trading_pair):
        return Decimal("50000")

    async def subscribe_orderbook(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield

        return _ctx()

    async def subscribe_trades(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield

        return _ctx()


class _MockFullGateway(_MockExecution, _MockMarketData):
    async def start(self):
        pass

    async def stop(self):
        pass

    @property
    def ready(self):
        return True


class _Incomplete:
    async def place_order(self, trading_pair, order_type, side, amount, price):
        return "order-1"


class TestExecutionGateway:
    def test_mock_satisfies_protocol(self) -> None:
        assert isinstance(_MockExecution(), ExecutionGateway)

    def test_incomplete_fails(self) -> None:
        assert not isinstance(_Incomplete(), ExecutionGateway)


class TestMarketDataGateway:
    def test_mock_satisfies_protocol(self) -> None:
        assert isinstance(_MockMarketData(), MarketDataGateway)

    def test_incomplete_fails(self) -> None:
        assert not isinstance(_Incomplete(), MarketDataGateway)


class TestExchangeGateway:
    def test_full_mock_satisfies(self) -> None:
        assert isinstance(_MockFullGateway(), ExchangeGateway)

    def test_execution_only_fails(self) -> None:
        assert not isinstance(_MockExecution(), ExchangeGateway)

    def test_market_data_only_fails(self) -> None:
        assert not isinstance(_MockMarketData(), ExchangeGateway)
