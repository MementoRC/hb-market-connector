# market_connector/contracts/protocols.py
"""Contract resolution protocol for structured-contract exchanges.

ContractResolver is distinct from SymbolMapper:
- SymbolMapper: synchronous, str -> str transform (no API call).
- ContractResolver: async API lookup + caching, returns ResolvedContract.

Spot-only exchanges (Coinbase, Kraken) use SymbolMapper.
Multi-asset exchanges (Interactive Brokers) use ContractResolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.primitives import ConnectorPair


@dataclass(frozen=True)
class ResolvedContract:
    """Result of an async contract resolution lookup."""

    ref: InstrumentRef
    native: Any
    contract_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContractResolver(Protocol):
    """Async lookup of native contract from a structured InstrumentRef.

    Implementations typically maintain an LRU cache and call the exchange's
    contract-details endpoint on miss. Caches must be invalidated on every
    reconnect cycle.

    By convention, an implementation of resolve_from_pair that does not
    support a canonical pair-string parse should raise NotImplementedError.
    Note this is a convention for implementers -- Protocols do not enforce
    method bodies at runtime.
    """

    async def resolve(self, ref: InstrumentRef) -> ResolvedContract: ...

    async def resolve_from_pair(self, pair: ConnectorPair) -> ResolvedContract: ...

    def invalidate(self, ref: InstrumentRef | None = None) -> None: ...
