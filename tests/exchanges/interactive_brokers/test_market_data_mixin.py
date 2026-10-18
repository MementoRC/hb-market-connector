"""Tests for MarketDataMixin.get_orderbook (Stage 3)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ContractNotFoundError,
    MarketDataTimeoutError,
)
from market_connector.exchanges.interactive_brokers.mixins.market_data import MarketDataMixin

_Levels = list[tuple[Decimal, Decimal]]


def _lv(price: str, size: str) -> tuple[Decimal, Decimal]:
    return Decimal(price), Decimal(size)


class FakeTransport:
    """Transport double exposing the contract the mixin delegates to.

    ``top_of_book`` / ``depth_snapshot`` return canned values (or raise ``error``) and record
    the contract they were called with. ``_ib`` is kept for get_candles.
    """

    def __init__(
        self,
        *,
        bid_ask: tuple[Decimal, Decimal] = (Decimal(0), Decimal(0)),
        depth: tuple[_Levels, _Levels] = ([], []),
        error: Exception | None = None,
    ) -> None:
        self._ib = MagicMock()
        self._bid_ask = bid_ask
        self._depth = depth
        self._error = error
        self.top_of_book_contracts: list[object] = []
        self.depth_snapshot_contracts: list[object] = []

    async def top_of_book(self, contract: object) -> tuple[Decimal, Decimal]:
        self.top_of_book_contracts.append(contract)
        if self._error is not None:
            raise self._error
        return self._bid_ask

    async def depth_snapshot(self, contract: object) -> tuple[_Levels, _Levels]:
        self.depth_snapshot_contracts.append(contract)
        if self._error is not None:
            raise self._error
        return self._depth


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
        resolver = FakeResolver()
        transport = FakeTransport(
            depth=(
                [_lv("150.0", "100.0"), _lv("149.99", "200.0")],
                [_lv("150.01", "50.0"), _lv("150.02", "75.0")],
            )
        )
        host = ConcreteMarketDataHost(transport, resolver)

        snapshot = await host.get_orderbook("AAPL-USD")

        assert snapshot.trading_pair == "AAPL-USD"
        assert len(snapshot.bids) == 2
        assert len(snapshot.asks) == 2
        assert snapshot.bids[0] == (Decimal("150.0"), Decimal("100.0"))
        assert snapshot.asks[0] == (Decimal("150.01"), Decimal("50.0"))
        assert transport.depth_snapshot_contracts == [resolver._contract]

    @pytest.mark.asyncio
    async def test_symbol_not_found_propagates(self) -> None:
        transport = FakeTransport()
        host = ConcreteMarketDataHost(transport, FakeResolver(raise_not_found=True))

        with pytest.raises(ContractNotFoundError):
            await host.get_orderbook("UNKNOWN-USD")
        assert transport.depth_snapshot_contracts == []

    @pytest.mark.asyncio
    async def test_transport_timeout_propagates(self) -> None:
        transport = FakeTransport(error=MarketDataTimeoutError(0, "no depth"))
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with pytest.raises(MarketDataTimeoutError):
            await host.get_orderbook("AAPL-USD")

    @pytest.mark.asyncio
    async def test_empty_depth_returns_empty_sided_snapshot(self) -> None:
        host = ConcreteMarketDataHost(FakeTransport(), FakeResolver())

        snapshot = await host.get_orderbook("AAPL-USD")

        assert snapshot.bids == []
        assert snapshot.asks == []


class TestGetMidPrice:
    @pytest.mark.asyncio
    async def test_mid_price_from_one_level_snapshot(self) -> None:
        resolver = FakeResolver()
        transport = FakeTransport(bid_ask=(Decimal("150.00"), Decimal("150.10")))
        host = ConcreteMarketDataHost(transport, resolver)

        mid = await host.get_mid_price("AAPL-USD")

        assert mid == (Decimal("150.00") + Decimal("150.10")) / 2
        assert transport.top_of_book_contracts == [resolver._contract]

    @pytest.mark.asyncio
    async def test_transport_timeout_propagates(self) -> None:
        transport = FakeTransport(error=MarketDataTimeoutError(0, "no top of book"))
        host = ConcreteMarketDataHost(transport, FakeResolver())

        with pytest.raises(MarketDataTimeoutError):
            await host.get_mid_price("AAPL-USD")

    @pytest.mark.asyncio
    async def test_symbol_not_found_propagates(self) -> None:
        transport = FakeTransport()
        host = ConcreteMarketDataHost(transport, FakeResolver(raise_not_found=True))

        with pytest.raises(ContractNotFoundError):
            await host.get_mid_price("UNKNOWN-USD")
        assert transport.top_of_book_contracts == []


class TestGetCandles:
    def _make_bar(
        self,
        date: str = "20240101 09:30:00",
        open: float = 150.0,
        high: float = 151.0,
        low: float = 149.5,
        close: float = 150.5,
        volume: float = 1000.0,
    ) -> MagicMock:
        bar = MagicMock()
        bar.date = date
        bar.open = open
        bar.high = high
        bar.low = low
        bar.close = close
        bar.volume = volume
        return bar

    @pytest.mark.asyncio
    async def test_happy_path_returns_candle_list(self) -> None:
        from market_connector.primitives import CandleData

        bars = [
            self._make_bar(
                date="20240101 09:30:00",
                open=150.0,
                high=151.0,
                low=149.5,
                close=150.5,
                volume=1000.0,
            ),
            self._make_bar(
                date="20240101 09:31:00",
                open=150.5,
                high=152.0,
                low=150.0,
                close=151.5,
                volume=800.0,
            ),
        ]

        resolver = FakeResolver()
        transport = FakeTransport()
        transport._ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        host = ConcreteMarketDataHost(transport, resolver)

        candles = await host.get_candles("AAPL-USD", interval="1m", limit=60)

        assert len(candles) == 2
        assert all(isinstance(c, CandleData) for c in candles)
        assert candles[0].open == Decimal("150.0")
        assert candles[0].interval == "1m"
        assert candles[0].trading_pair == "AAPL-USD"

    @pytest.mark.asyncio
    async def test_negative_one_volume_coerced_to_zero(self) -> None:
        bars = [self._make_bar(volume=-1.0)]
        resolver = FakeResolver()
        transport = FakeTransport()
        transport._ib.reqHistoricalDataAsync = AsyncMock(return_value=bars)
        host = ConcreteMarketDataHost(transport, resolver)

        candles = await host.get_candles("AAPL-USD", interval="1m", limit=5)

        assert candles[0].volume == Decimal("0")

    @pytest.mark.asyncio
    async def test_invalid_interval_raises(self) -> None:
        from market_connector.exchanges.interactive_brokers.interval_map import InvalidIntervalError

        resolver = FakeResolver()
        transport = FakeTransport()
        host = ConcreteMarketDataHost(transport, resolver)

        with pytest.raises(InvalidIntervalError):
            await host.get_candles("AAPL-USD", interval="99m", limit=5)
