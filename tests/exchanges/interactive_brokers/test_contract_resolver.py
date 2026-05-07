"""Tests for IbContractResolver — cache, ambiguity policy, parse-then-resolve."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.contract_resolver import (
    _MAX_CACHE,
    IbContractResolver,
)
from market_connector.exchanges.interactive_brokers.exceptions import (
    AmbiguousContractError,
    ContractNotFoundError,
)


def _make_contract_details(con_id: int = 1, symbol: str = "AAPL") -> MagicMock:
    """Return a minimal ib_async ContractDetails mock."""
    details = MagicMock()
    details.contract.conId = con_id
    details.contract.symbol = symbol
    details.contract.primaryExchange = "NASDAQ"
    details.tradingHours = "20241206:0930-1600"
    return details


def _make_transport(details_list: list) -> MagicMock:
    """Return a transport mock whose _resolve_via_ib returns details_list."""
    transport = MagicMock()
    transport._resolve_via_ib = AsyncMock(return_value=details_list)
    return transport


@pytest.fixture
def aapl_ref() -> InstrumentRef:
    return InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK, quote_currency="USD")


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_cache_miss_calls_transport(self, aapl_ref):
        details = _make_contract_details()
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        result = await resolver.resolve(aapl_ref)

        transport._resolve_via_ib.assert_awaited_once_with(aapl_ref)
        assert result.contract_id == "1"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_transport(self, aapl_ref):
        details = _make_contract_details()
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        first = await resolver.resolve(aapl_ref)
        second = await resolver.resolve(aapl_ref)

        # Transport called only once — second call is a cache hit.
        transport._resolve_via_ib.assert_awaited_once()
        assert first is second

    @pytest.mark.asyncio
    async def test_cache_populated_with_resolved_contract(self, aapl_ref):
        details = _make_contract_details(con_id=42)
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        await resolver.resolve(aapl_ref)
        assert aapl_ref in resolver._cache
        assert resolver._cache[aapl_ref].contract_id == "42"

    @pytest.mark.asyncio
    async def test_fifo_eviction_at_max_capacity(self):
        """Adding entry 1025 evicts entry 0 (oldest) — FIFO ordering."""
        transport = _make_transport([_make_contract_details()])
        resolver = IbContractResolver(transport)

        # Pre-fill cache to exactly _MAX_CACHE entries.
        for i in range(_MAX_CACHE):
            ref = InstrumentRef(
                symbol=f"SYM{i}", instrument_type=InstrumentType.STOCK, quote_currency="USD"
            )
            details = _make_contract_details(con_id=i, symbol=f"SYM{i}")
            transport._resolve_via_ib = AsyncMock(return_value=[details])
            await resolver.resolve(ref)

        assert len(resolver._cache) == _MAX_CACHE
        first_key = next(iter(resolver._cache))

        # One more unique entry triggers eviction.
        new_ref = InstrumentRef(
            symbol="NEWENTRY", instrument_type=InstrumentType.STOCK, quote_currency="USD"
        )
        new_details = _make_contract_details(con_id=9999, symbol="NEWENTRY")
        transport._resolve_via_ib = AsyncMock(return_value=[new_details])
        await resolver.resolve(new_ref)

        assert len(resolver._cache) == _MAX_CACHE
        assert first_key not in resolver._cache
        assert new_ref in resolver._cache


class TestAmbiguityAndNotFound:
    @pytest.mark.asyncio
    async def test_not_found_raises_contract_not_found(self, aapl_ref):
        transport = _make_transport([])
        resolver = IbContractResolver(transport)

        with pytest.raises(ContractNotFoundError):
            await resolver.resolve(aapl_ref)

    @pytest.mark.asyncio
    async def test_ambiguous_raises_ambiguous_contract_error(self, aapl_ref):
        details_a = _make_contract_details(con_id=1)
        details_b = _make_contract_details(con_id=2)
        transport = _make_transport([details_a, details_b])
        resolver = IbContractResolver(transport)

        with pytest.raises(AmbiguousContractError) as exc_info:
            await resolver.resolve(aapl_ref)
        assert len(exc_info.value.matches) == 2


class TestParsePair:
    def test_parse_pair_symbol_dash_currency(self):
        from market_connector.exchanges.interactive_brokers.contract_resolver import (
            IbContractResolver,
        )

        ref = IbContractResolver._parse_pair("AAPL-USD")
        assert ref.symbol == "AAPL"
        assert ref.quote_currency == "USD"
        assert ref.instrument_type == InstrumentType.STOCK

    def test_parse_pair_no_dash(self):
        from market_connector.exchanges.interactive_brokers.contract_resolver import (
            IbContractResolver,
        )

        ref = IbContractResolver._parse_pair("AAPL")
        assert ref.symbol == "AAPL"
        assert ref.instrument_type == InstrumentType.STOCK

    @pytest.mark.asyncio
    async def test_resolve_from_pair_delegates_to_resolve(self, aapl_ref):
        details = _make_contract_details()
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        # resolve_from_pair parses then calls resolve; result should match direct resolve.
        result = await resolver.resolve_from_pair("AAPL-USD")

        transport._resolve_via_ib.assert_awaited_once()
        assert result.contract_id == "1"


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_specific_ref_removes_entry(self, aapl_ref):
        details = _make_contract_details()
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        await resolver.resolve(aapl_ref)
        assert aapl_ref in resolver._cache

        resolver.invalidate(aapl_ref)
        assert aapl_ref not in resolver._cache

    @pytest.mark.asyncio
    async def test_invalidate_none_clears_all(self, aapl_ref):
        details = _make_contract_details()
        transport = _make_transport([details])
        resolver = IbContractResolver(transport)

        await resolver.resolve(aapl_ref)
        assert len(resolver._cache) == 1

        resolver.invalidate()
        assert len(resolver._cache) == 0

    def test_invalidate_missing_ref_is_noop(self, aapl_ref):
        transport = _make_transport([])
        resolver = IbContractResolver(transport)
        # Should not raise even when ref is absent.
        resolver.invalidate(aapl_ref)


class TestDoubleCheckedLocking:
    @pytest.mark.asyncio
    async def test_concurrent_resolves_call_transport_once(self, aapl_ref):
        """Two concurrent resolves for the same key should result in only one IB call."""
        call_count = 0

        async def slow_resolve(ref):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return [_make_contract_details()]

        transport = MagicMock()
        transport._resolve_via_ib = slow_resolve
        resolver = IbContractResolver(transport)

        results = await asyncio.gather(
            resolver.resolve(aapl_ref),
            resolver.resolve(aapl_ref),
        )

        assert call_count == 1
        assert results[0] is results[1]
