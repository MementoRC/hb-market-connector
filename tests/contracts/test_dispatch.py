"""Tests for native_for() contracts dispatch helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from market_connector.contracts.dispatch import native_for
from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.contracts.protocols import ResolvedContract

if TYPE_CHECKING:
    from market_connector.primitives import ConnectorPair


class _StubResolver:
    async def resolve(self, ref: InstrumentRef) -> ResolvedContract:
        return ResolvedContract(ref=ref, native=f"NATIVE:{ref.symbol}", contract_id=ref.symbol)

    async def resolve_from_pair(self, pair: ConnectorPair) -> ResolvedContract:
        return ResolvedContract(
            ref=InstrumentRef(symbol=str(pair), instrument_type=InstrumentType.STOCK),
            native=f"NATIVE_FROM_PAIR:{pair}",
            contract_id=str(pair),
        )

    def invalidate(self, ref: InstrumentRef | None = None) -> None:
        pass


class _StubMapper:
    def map(self, pair: ConnectorPair) -> str:
        return f"MAPPED:{pair}"


async def _noop_async(*args, **kwargs):
    pass


class _ResolverGateway:
    contract_resolver = _StubResolver()
    symbol_mapper = None

    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    place_order = _noop_async
    cancel_order = _noop_async
    get_open_orders = _noop_async
    get_balance = _noop_async
    get_orderbook = _noop_async
    get_candles = _noop_async
    get_mid_price = _noop_async
    subscribe_orderbook = _noop_async
    subscribe_trades = _noop_async


class _MapperGateway:
    contract_resolver = None
    symbol_mapper = _StubMapper()

    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    place_order = _noop_async
    cancel_order = _noop_async
    get_open_orders = _noop_async
    get_balance = _noop_async
    get_orderbook = _noop_async
    get_candles = _noop_async
    get_mid_price = _noop_async
    subscribe_orderbook = _noop_async
    subscribe_trades = _noop_async


class _NoneGateway:
    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    place_order = _noop_async
    cancel_order = _noop_async
    get_open_orders = _noop_async
    get_balance = _noop_async
    get_orderbook = _noop_async
    get_candles = _noop_async
    get_mid_price = _noop_async
    subscribe_orderbook = _noop_async
    subscribe_trades = _noop_async


class TestNativeForResolverPath:
    @pytest.mark.asyncio
    async def test_instrument_ref_uses_resolve(self):
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        result = await native_for(_ResolverGateway(), ref)
        assert result == "NATIVE:AAPL"

    @pytest.mark.asyncio
    async def test_connector_pair_uses_resolve_from_pair(self):
        pair: ConnectorPair = "AAPL-USD"
        result = await native_for(_ResolverGateway(), pair)
        assert result == "NATIVE_FROM_PAIR:AAPL-USD"


class TestNativeForMapperFallback:
    @pytest.mark.asyncio
    async def test_connector_pair_uses_mapper(self):
        pair: ConnectorPair = "BTC-USD"
        result = await native_for(_MapperGateway(), pair)
        assert result == "MAPPED:BTC-USD"

    @pytest.mark.asyncio
    async def test_instrument_ref_raises_when_no_resolver(self):
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        with pytest.raises(ValueError, match="no ContractResolver"):
            await native_for(_MapperGateway(), ref)


class TestNativeForNoBackend:
    @pytest.mark.asyncio
    async def test_raises_when_neither_resolver_nor_mapper(self):
        pair: ConnectorPair = "X-Y"
        with pytest.raises(ValueError, match="neither contract_resolver nor symbol_mapper"):
            await native_for(_NoneGateway(), pair)
