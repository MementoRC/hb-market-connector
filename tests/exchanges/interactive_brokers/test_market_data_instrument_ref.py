"""Acceptance tests for issue #102: IB market-data APIs accept an InstrumentRef or a pair string.

Covers get_orderbook, get_mid_price, get_candles, subscribe_orderbook, subscribe_trades.
A ref goes through ``resolve(ref)``; a pair string through ``resolve_from_pair(pair)``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.protocols import MarketDataGateway

from .test_market_data_mixin import ConcreteMarketDataHost, FakeTransport
from .test_subscriptions_mixin import ConcreteSubscriptionsHost
from .test_subscriptions_mixin import FakeTransport as FakeSubTransport

_CRYPTO = InstrumentRef(
    symbol="BTC",
    instrument_type=InstrumentType.CRYPTO,
    quote_currency="USD",
    exchange_hint="PAXOS",
)
_FOREX = InstrumentRef(
    symbol="EUR",
    instrument_type=InstrumentType.FOREX,
    quote_currency="USD",
    exchange_hint="IDEALPRO",
)
_FUT = InstrumentRef(
    symbol="ES",
    instrument_type=InstrumentType.FUTURE,
    quote_currency="USD",
    exchange_hint="CME",
    contract_month="202612",
)

_REFS = [
    pytest.param(_CRYPTO, "BTC-USD", id="crypto"),
    pytest.param(_FOREX, "EUR-USD", id="forex"),
    pytest.param(_FUT, "ES-USD", id="future"),
]


class RecordingResolver:
    """Records resolve(ref) and resolve_from_pair(pair) calls; distinct contract per call."""

    def __init__(self) -> None:
        self.resolve_calls: list[InstrumentRef] = []
        self.pair_calls: list[str] = []
        self.returned: list[MagicMock] = []

    def _new_contract(self) -> MagicMock:
        contract = MagicMock()
        contract.conId = 1000 + len(self.returned)
        self.returned.append(contract)
        return contract

    async def resolve(self, ref: InstrumentRef) -> MagicMock:
        self.resolve_calls.append(ref)
        return self._new_contract()

    async def resolve_from_pair(self, pair: str) -> MagicMock:
        self.pair_calls.append(pair)
        return self._new_contract()


def _bar() -> MagicMock:
    bar = MagicMock()
    bar.date = "20240101 09:30:00"
    bar.open, bar.high, bar.low, bar.close, bar.volume = 1.0, 2.0, 0.5, 1.5, 10.0
    return bar


def _market_host() -> tuple[ConcreteMarketDataHost, FakeTransport, RecordingResolver]:
    resolver = RecordingResolver()
    transport = FakeTransport(bid_ask=(Decimal("100"), Decimal("102")))
    transport._ib.reqHistoricalDataAsync = AsyncMock(return_value=[_bar(), _bar()])
    return ConcreteMarketDataHost(transport, resolver), transport, resolver  # type: ignore[arg-type]


def _sub_host() -> tuple[ConcreteSubscriptionsHost, FakeSubTransport, RecordingResolver]:
    resolver = RecordingResolver()
    host = ConcreteSubscriptionsHost()
    host._contract_resolver = resolver  # type: ignore[assignment]
    return host, host._transport, resolver  # type: ignore[return-value]


def _tick() -> MagicMock:
    tick = MagicMock()
    tick.time = datetime(2024, 1, 1, 9, 30, 0)
    tick.price = 150.5
    tick.size = 10
    return tick


def _assert_ref_resolved(resolver: RecordingResolver, ref: InstrumentRef) -> MagicMock:
    assert resolver.pair_calls == []
    assert resolver.resolve_calls == [ref]
    return resolver.returned[0]


class TestRefOnMarketData:
    @pytest.mark.parametrize(("ref", "label"), _REFS)
    async def test_get_mid_price_uses_resolve(self, ref: InstrumentRef, label: str) -> None:
        host, transport, resolver = _market_host()

        mid = await host.get_mid_price(ref)  # type: ignore[arg-type]

        contract = _assert_ref_resolved(resolver, ref)
        assert transport.top_of_book_contracts == [contract]
        assert mid == Decimal(101)

    @pytest.mark.parametrize(("ref", "label"), _REFS)
    async def test_get_orderbook_uses_resolve_and_labels(
        self, ref: InstrumentRef, label: str
    ) -> None:
        host, transport, resolver = _market_host()

        snapshot = await host.get_orderbook(ref)  # type: ignore[arg-type]

        contract = _assert_ref_resolved(resolver, ref)
        assert transport.depth_snapshot_contracts == [contract]
        assert snapshot.trading_pair == label

    @pytest.mark.parametrize(("ref", "label"), _REFS)
    async def test_get_candles_uses_resolve_and_labels(
        self, ref: InstrumentRef, label: str
    ) -> None:
        host, transport, resolver = _market_host()

        candles = await host.get_candles(ref, interval="1m", limit=2)  # type: ignore[arg-type]

        contract = _assert_ref_resolved(resolver, ref)
        assert transport._ib.reqHistoricalDataAsync.call_args.args[0] is contract
        assert len(candles) == 2
        assert all(c.trading_pair == label for c in candles)

    async def test_label_without_quote_currency_is_symbol(self) -> None:
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        host, _, _ = _market_host()

        snapshot = await host.get_orderbook(ref)  # type: ignore[arg-type]
        candles = await host.get_candles(ref, interval="1m", limit=2)  # type: ignore[arg-type]

        assert snapshot.trading_pair == "AAPL"
        assert all(c.trading_pair == "AAPL" for c in candles)

    async def test_keyword_trading_pair_accepts_ref(self) -> None:
        host, _, resolver = _market_host()

        await host.get_mid_price(trading_pair=_CRYPTO)  # type: ignore[arg-type]

        assert resolver.resolve_calls == [_CRYPTO]


class TestRefOnSubscriptions:
    @pytest.mark.parametrize(("ref", "label"), _REFS)
    async def test_subscribe_orderbook(self, ref: InstrumentRef, label: str) -> None:
        from market_connector.exchanges.interactive_brokers.transport import DepthBook

        host, transport, resolver = _sub_host()
        received: list = []

        async with host.subscribe_orderbook(ref, received.append):  # type: ignore[arg-type]
            transport._ticker.updateEvent(DepthBook(bids=(), asks=()))

        contract = _assert_ref_resolved(resolver, ref)
        assert transport._subscriptions[0][:2] == ("depth", contract)
        assert [u.trading_pair for u in received] == [label]

    @pytest.mark.parametrize(("ref", "label"), _REFS)
    async def test_subscribe_trades(self, ref: InstrumentRef, label: str) -> None:
        host, transport, resolver = _sub_host()
        received: list = []

        async with host.subscribe_trades(ref, received.append):  # type: ignore[arg-type]
            transport._ticker.tickByTickAllLastEvent(_tick())

        contract = _assert_ref_resolved(resolver, ref)
        assert transport._subscriptions[0][:2] == ("trades", contract)
        assert [t.trading_pair for t in received] == [label]

    async def test_label_without_quote_currency_is_symbol(self) -> None:
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        host, transport, _ = _sub_host()
        received: list = []

        async with host.subscribe_trades(ref, received.append):  # type: ignore[arg-type]
            transport._ticker.tickByTickAllLastEvent(_tick())

        assert [t.trading_pair for t in received] == ["AAPL"]


class TestPairStringRegression:
    async def test_market_data_methods_use_resolve_from_pair(self) -> None:
        host, transport, resolver = _market_host()

        await host.get_mid_price("AAPL-USD")
        snapshot = await host.get_orderbook("AAPL-USD")
        candles = await host.get_candles("AAPL-USD", interval="1m", limit=2)

        assert resolver.resolve_calls == []
        assert resolver.pair_calls == ["AAPL-USD"] * 3
        assert transport.top_of_book_contracts == [resolver.returned[0]]
        assert snapshot.trading_pair == "AAPL-USD"
        assert all(c.trading_pair == "AAPL-USD" for c in candles)

    async def test_subscriptions_use_resolve_from_pair(self) -> None:
        from market_connector.exchanges.interactive_brokers.transport import DepthBook

        host, transport, resolver = _sub_host()
        books: list = []
        trades: list = []

        async with host.subscribe_orderbook("AAPL-USD", books.append):
            transport._ticker.updateEvent(DepthBook(bids=(), asks=()))
        async with host.subscribe_trades("AAPL-USD", trades.append):
            transport._ticker.tickByTickAllLastEvent(_tick())

        assert resolver.resolve_calls == []
        assert resolver.pair_calls == ["AAPL-USD", "AAPL-USD"]
        assert [u.trading_pair for u in books] == ["AAPL-USD"]
        assert [t.trading_pair for t in trades] == ["AAPL-USD"]


def test_gateway_still_satisfies_market_data_protocol() -> None:
    gateway = IbGatewayGateway.__new__(IbGatewayGateway)
    assert isinstance(gateway, MarketDataGateway)
