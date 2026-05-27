"""Tests for MarketDataMixin.get_orderbook (Stage 3)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ContractNotFoundError,
    MarketDataPermissionError,
)
from market_connector.exchanges.interactive_brokers.mixins.market_data import MarketDataMixin


def _make_dom_level(price: float, size: float) -> MagicMock:
    level = MagicMock()
    level.price = price
    level.size = size
    return level


def _make_fake_ticker(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> MagicMock:
    ticker = MagicMock()
    ticker.domBids = [_make_dom_level(p, s) for p, s in bids]
    ticker.domAsks = [_make_dom_level(p, s) for p, s in asks]
    return ticker


class FakeTransport:
    """Minimal transport double: _ib exposes reqMktDepth / cancelMktDepth."""

    def __init__(self, ticker: MagicMock) -> None:
        self._ib = MagicMock()
        self._ib.reqMktDepth.return_value = ticker
        self._cancel_called = False

        def cancel_side_effect(*args, **kwargs) -> None:
            self._cancel_called = True

        self._ib.cancelMktDepth.side_effect = cancel_side_effect


class FakeResolver:
    """Resolver double: returns a fake contract or raises SymbolNotFoundError."""

    def __init__(self, *, raise_not_found: bool = False) -> None:
        self._raise = raise_not_found
        self._contract = MagicMock()
        self._contract.conId = 265598

    async def resolve_from_pair(self, pair: str):  # noqa: ANN201
        if self._raise:
            raise ContractNotFoundError(200, f"No security definition for {pair}")
        return self._contract


class ConcreteMarketDataHost(MarketDataMixin):
    """Concrete host composing MarketDataMixin for test purposes."""

    def __init__(self, transport: FakeTransport, resolver: FakeResolver) -> None:
        self._transport = transport
        self._contract_resolver = resolver
        self._ready = True

    async def ensure_ready(self) -> None:
        pass


class TestGetOrderbook:
    @pytest.mark.asyncio
    async def test_happy_path_returns_snapshot(self) -> None:
        ticker = _make_fake_ticker(
            bids=[(150.00, 100.0), (149.99, 200.0)],
            asks=[(150.01, 50.0), (150.02, 75.0)],
        )
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with patch("asyncio.sleep", new_callable=AsyncMock):
            snapshot = await host.get_orderbook("AAPL-USD")

        assert snapshot.trading_pair == "AAPL-USD"
        assert len(snapshot.bids) == 2
        assert len(snapshot.asks) == 2
        assert snapshot.bids[0] == (Decimal("150.0"), Decimal("100.0"))
        assert snapshot.asks[0] == (Decimal("150.01"), Decimal("50.0"))

    @pytest.mark.asyncio
    async def test_symbol_not_found_propagates(self) -> None:
        ticker = _make_fake_ticker([], [])
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver(raise_not_found=True))

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(ContractNotFoundError):
            await host.get_orderbook("UNKNOWN-USD")

    @pytest.mark.asyncio
    async def test_cancel_mkt_depth_called_in_finally(self) -> None:
        """cancelMktDepth must be called even when asyncio.sleep is cancelled."""
        ticker = _make_fake_ticker([], [])
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        async def raise_on_sleep(*args, **kwargs) -> None:
            raise asyncio.CancelledError

        with (
            patch("asyncio.sleep", side_effect=raise_on_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await host.get_orderbook("AAPL-USD")

        assert transport._cancel_called, "cancelMktDepth was not called in finally"

    @pytest.mark.asyncio
    async def test_empty_depth_returns_empty_sided_snapshot(self) -> None:
        ticker = _make_fake_ticker([], [])
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with patch("asyncio.sleep", new_callable=AsyncMock):
            snapshot = await host.get_orderbook("AAPL-USD")

        assert snapshot.bids == []
        assert snapshot.asks == []


class TestGetMidPrice:
    @pytest.mark.asyncio
    async def test_mid_price_from_one_level_snapshot(self) -> None:
        ticker = _make_fake_ticker(
            bids=[(150.00, 100.0)],
            asks=[(150.10, 50.0)],
        )
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with patch("asyncio.sleep", new_callable=AsyncMock):
            mid = await host.get_mid_price("AAPL-USD")

        expected = (Decimal("150.00") + Decimal("150.10")) / 2
        assert mid == expected

    @pytest.mark.asyncio
    async def test_empty_bids_raises_market_data_permission_error(self) -> None:
        ticker = _make_fake_ticker(bids=[], asks=[(150.01, 50.0)])
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(MarketDataPermissionError, match="no market data"),
        ):
            await host.get_mid_price("AAPL-USD")

    @pytest.mark.asyncio
    async def test_empty_asks_raises_market_data_permission_error(self) -> None:
        ticker = _make_fake_ticker(bids=[(150.00, 100.0)], asks=[])
        transport = FakeTransport(ticker)
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(MarketDataPermissionError, match="no market data"),
        ):
            await host.get_mid_price("AAPL-USD")
