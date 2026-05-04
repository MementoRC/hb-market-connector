"""Tests for ContractResolver Protocol and ResolvedContract record."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.contracts.protocols import ContractResolver, ResolvedContract

if TYPE_CHECKING:
    from market_connector.primitives import ConnectorPair


class TestResolvedContract:
    def test_construction(self):
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        rc = ResolvedContract(
            ref=ref,
            native={"conId": 265598},
            contract_id="265598",
            metadata={"primary_exchange": "NASDAQ"},
        )
        assert rc.ref == ref
        assert rc.contract_id == "265598"
        assert rc.metadata["primary_exchange"] == "NASDAQ"

    def test_metadata_default_empty(self):
        ref = InstrumentRef(symbol="A", instrument_type=InstrumentType.STOCK)
        rc = ResolvedContract(ref=ref, native=None, contract_id="0")
        assert rc.metadata == {}


class _StubResolver:
    """Minimal structural implementation of ContractResolver."""

    def __init__(self):
        self.invalidated_calls: list[Any] = []

    async def resolve(self, ref: InstrumentRef) -> ResolvedContract:
        return ResolvedContract(ref=ref, native=None, contract_id=ref.symbol)

    async def resolve_from_pair(self, pair: ConnectorPair) -> ResolvedContract:
        raise NotImplementedError("stub")

    def invalidate(self, ref: InstrumentRef | None = None) -> None:
        self.invalidated_calls.append(ref)


class TestContractResolverProtocol:
    def test_stub_satisfies_protocol(self):
        r = _StubResolver()
        assert isinstance(r, ContractResolver)

    @pytest.mark.asyncio
    async def test_resolve_returns_resolved_contract(self):
        r = _StubResolver()
        ref = InstrumentRef(symbol="X", instrument_type=InstrumentType.STOCK)
        rc = await r.resolve(ref)
        assert rc.ref == ref
        assert rc.contract_id == "X"

    def test_invalidate_records_call(self):
        r = _StubResolver()
        r.invalidate()
        r.invalidate(InstrumentRef(symbol="A", instrument_type=InstrumentType.STOCK))
        assert len(r.invalidated_calls) == 2
        assert r.invalidated_calls[0] is None
