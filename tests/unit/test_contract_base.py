# tests/unit/test_contract_base.py
"""Verify the contract test base works with a mock gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

import pytest

from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)
from market_connector.exceptions import GatewayNotStartedError
from market_connector.protocols import ExchangeGateway
from market_connector.testing.contract import GatewayContractTestBase


class _InMemoryGateway:
    """Minimal in-memory gateway for testing the contract base."""

    def __init__(self) -> None:
        self._started = False
        self._orders: dict[str, OpenOrder] = {}

    def _check_started(self) -> None:
        if not self._started:
            raise GatewayNotStartedError("gateway not started")

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started

    async def place_order(self, trading_pair, order_type, side, amount, price):
        self._check_started()
        oid = f"order-{len(self._orders)}"
        self._orders[oid] = OpenOrder(
            client_order_id=oid,
            exchange_order_id=f"ex-{oid}",
            trading_pair=trading_pair,
            order_type=order_type,
            side=side,
            amount=amount,
            price=price or Decimal("0"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        return oid

    async def cancel_order(self, trading_pair, client_order_id):
        return self._orders.pop(client_order_id, None) is not None

    async def get_open_orders(self, trading_pair):
        return [o for o in self._orders.values() if o.trading_pair == trading_pair]

    async def get_balance(self, currency):
        return Decimal("10000")

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


class TestInMemoryGatewayContract(GatewayContractTestBase):
    """Run the full contract suite against the in-memory mock."""

    @pytest.fixture
    def gateway(self) -> _InMemoryGateway:
        return _InMemoryGateway()

    @pytest.fixture
    def trading_pair(self) -> str:
        return "BTC-USDT"
