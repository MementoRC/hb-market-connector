"""IB contract resolver — wraps reqContractDetails with cache + ambiguity policy."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.exceptions import (
    AmbiguousContractError,
    ContractNotFoundError,
)

if TYPE_CHECKING:
    from market_connector.contracts.protocols import ResolvedContract
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


_MAX_CACHE = 1024

# Map InstrumentType → IB secType string.
_INSTRUMENT_TYPE_TO_SECTYPE: dict[InstrumentType, str] = {
    InstrumentType.STOCK: "STK",
    InstrumentType.OPTION: "OPT",
    InstrumentType.FUTURE: "FUT",
    InstrumentType.FOREX: "CASH",
    InstrumentType.CRYPTO: "CRYPTO",
    InstrumentType.BOND: "BOND",
    InstrumentType.CFD: "CFD",
    InstrumentType.INDEX: "IND",
}


class IbContractResolver:
    """Resolves InstrumentRef → ResolvedContract via ib_async reqContractDetails.

    Uses a manual asyncio-Lock-protected cache (max _MAX_CACHE entries, FIFO eviction).
    functools.lru_cache is not used because it caches coroutine objects on async methods,
    not the awaited results. Double-checked locking serializes concurrent cache misses
    for the same key without blocking independent keys unnecessarily within Stage 2
    (single shared lock is acceptable for <100 instruments typical in one session).
    """

    def __init__(self, transport: IbGatewayTransport) -> None:
        self._transport = transport
        self._cache: OrderedDict[InstrumentRef, ResolvedContract] = OrderedDict()
        self._lock = asyncio.Lock()

    async def resolve(self, ref: InstrumentRef) -> ResolvedContract:
        """Resolve an InstrumentRef to a ResolvedContract.

        Cache hit returns immediately without acquiring the lock. On miss, the
        lock serializes concurrent resolves for the same key (double-checked).
        """
        # Fast path — no lock required for cache hit.
        cached = self._cache.get(ref)
        if cached is not None:
            return cached

        async with self._lock:
            # Double-checked: another coroutine may have populated while we waited.
            cached = self._cache.get(ref)
            if cached is not None:
                return cached

            details_list = await self._transport._resolve_via_ib(ref)

            if len(details_list) == 0:
                raise ContractNotFoundError(200, f"No security definition for {ref}")
            if len(details_list) > 1:
                raise AmbiguousContractError(matches=details_list)

            details = details_list[0]
            # Import here to avoid circular dependency at module level.
            from market_connector.contracts.protocols import ResolvedContract  # noqa: PLC0415

            resolved = ResolvedContract(
                ref=ref,
                native=details.contract,
                contract_id=str(details.contract.conId),
                metadata={
                    "primary_exchange": getattr(details.contract, "primaryExchange", None),
                    "trading_hours": getattr(details, "tradingHours", None),
                },
            )

            if len(self._cache) >= _MAX_CACHE:
                self._cache.popitem(last=False)  # FIFO eviction

            self._cache[ref] = resolved
            return resolved

    async def resolve_from_pair(self, pair: str) -> ResolvedContract:
        """Parse a 'SYMBOL-CURRENCY' pair string and delegate to resolve()."""
        ref = self._parse_pair(pair)
        return await self.resolve(ref)

    def invalidate(self, ref: InstrumentRef | None = None) -> None:
        """Remove a specific entry from the cache, or clear all entries."""
        if ref is None:
            self._cache.clear()
        else:
            self._cache.pop(ref, None)

    @staticmethod
    def _parse_pair(pair: str) -> InstrumentRef:
        """Parse 'AAPL-USD' → InstrumentRef(symbol='AAPL', STOCK, quote_currency='USD').

        Stage 2 assumes all pairs are STOCK instruments. Future stages may extend
        this heuristic via secType hints in the pair string or a separate parse config.
        """
        if "-" not in pair:
            return InstrumentRef(symbol=pair, instrument_type=InstrumentType.STOCK)
        symbol, _, quote = pair.partition("-")
        return InstrumentRef(
            symbol=symbol, instrument_type=InstrumentType.STOCK, quote_currency=quote
        )
