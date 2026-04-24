"""Abstract contract test base for gateway conformance.

Connector packages subclass this and provide a gateway fixture.
The contract tests validate that the gateway correctly implements
the ExchangeGateway protocol.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from market_connector.primitives import OpenOrder, OrderBookSnapshot
from market_connector.protocols import ExchangeGateway


class GatewayContractTestBase:
    """Abstract base: subclass and provide `gateway` and `trading_pair` fixtures.

    Each test method validates one aspect of the ExchangeGateway contract.
    Trading pair format validation (BASE-QUOTE canonical) is a per-connector
    responsibility, not tested here.
    """

    @pytest.fixture
    def gateway(self) -> ExchangeGateway:
        raise NotImplementedError("Subclass must provide a gateway fixture")

    @pytest.fixture
    def trading_pair(self) -> str:
        raise NotImplementedError("Subclass must provide a trading_pair fixture")

    # --- Lifecycle ---

    @pytest.mark.asyncio
    async def test_start_sets_ready(self, gateway: ExchangeGateway) -> None:
        assert not gateway.ready
        await gateway.start()
        assert gateway.ready

    @pytest.mark.asyncio
    async def test_stop_clears_ready(self, gateway: ExchangeGateway) -> None:
        """stop() MUST set ready=False per spec Connection Lifecycle section."""
        await gateway.start()
        await gateway.stop()
        assert not gateway.ready

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, gateway: ExchangeGateway) -> None:
        await gateway.start()
        await gateway.stop()
        await gateway.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_method_before_start_raises(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        """Any gateway method called before start() must raise GatewayNotStartedError."""
        from market_connector.exceptions import GatewayNotStartedError

        with pytest.raises(GatewayNotStartedError):
            await gateway.place_order(trading_pair, "LIMIT", "BUY", Decimal("1"), Decimal("50000"))

    # --- Execution ---

    @pytest.mark.asyncio
    async def test_place_order_returns_client_id(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair, "LIMIT", "BUY", Decimal("1.0"), Decimal("50000"),
        )
        assert isinstance(order_id, str)
        assert len(order_id) > 0

    @pytest.mark.asyncio
    async def test_cancel_order_returns_bool(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair, "LIMIT", "BUY", Decimal("1.0"), Decimal("50000"),
        )
        result = await gateway.cancel_order(trading_pair, order_id)
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_list(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        orders = await gateway.get_open_orders(trading_pair)
        assert isinstance(orders, list)

    @pytest.mark.asyncio
    async def test_get_balance_returns_decimal(
        self, gateway: ExchangeGateway,
    ) -> None:
        await gateway.start()
        balance = await gateway.get_balance("USDT")
        assert isinstance(balance, Decimal)

    # --- Market Data ---

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_snapshot(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        book = await gateway.get_orderbook(trading_pair)
        assert isinstance(book, OrderBookSnapshot)
        assert book.trading_pair == trading_pair

    @pytest.mark.asyncio
    async def test_get_mid_price_returns_decimal(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        price = await gateway.get_mid_price(trading_pair)
        assert isinstance(price, Decimal)
        assert price > 0

    @pytest.mark.asyncio
    async def test_get_candles_returns_list(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        candles = await gateway.get_candles(trading_pair, "1m", 10)
        assert isinstance(candles, list)
