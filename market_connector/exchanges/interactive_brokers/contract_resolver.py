"""IB contract resolver — wraps reqContractDetails with cache + ambiguity policy."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.exceptions import (
    AmbiguousContractError,
    ContractNotFoundError,
)

if TYPE_CHECKING:
    from market_connector.contracts.protocols import ResolvedContract
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


_MAX_CACHE = 1024
_MAX_CANDIDATES = 5
_MATCH_TIMEOUT_S = 3.0


def _format_contract(contract: Any, *, with_exchange: bool = False) -> str:
    """Format a contract as 'SYM SECTYPE [EXCH] PRIMARY CCY conId=N' for error messages."""
    parts = [contract.symbol, contract.secType]
    if with_exchange:
        parts.append(contract.exchange)
    parts += [contract.primaryExchange, contract.currency, f"conId={contract.conId}"]
    return " ".join(str(p) for p in parts)


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

# Default IB exchange per InstrumentType when no exchange_hint is given.
# Types not listed fall back to "SMART"; FUTURE has no default (exchange required).
_INSTRUMENT_TYPE_TO_DEFAULT_EXCHANGE: dict[InstrumentType, str] = {
    InstrumentType.CRYPTO: "PAXOS",
    InstrumentType.FOREX: "IDEALPRO",
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
        self._by_con_id: dict[int, ResolvedContract] = {}
        self._lock = asyncio.Lock()

    def _lookup(self, ref: InstrumentRef) -> ResolvedContract | None:
        """Return a cached contract by ref, falling back to the conId index."""
        cached = self._cache.get(ref)
        if cached is None and ref.con_id is not None:
            cached = self._by_con_id.get(ref.con_id)
        return cached

    def _drop_con_id(self, resolved: ResolvedContract) -> None:
        """Remove resolved's conId from the index unless another cached entry shares it."""
        con_id = int(resolved.contract_id)
        if not any(int(r.contract_id) == con_id for r in self._cache.values()):
            self._by_con_id.pop(con_id, None)

    async def resolve(self, ref: InstrumentRef) -> ResolvedContract:
        """Resolve an InstrumentRef to a ResolvedContract.

        Cache hit (by ref or by conId) returns immediately without acquiring the
        lock. On miss, the lock serializes concurrent resolves for the same key
        (double-checked).
        """
        # Fast path — no lock required for cache hit.
        cached = self._lookup(ref)
        if cached is not None:
            return cached

        async with self._lock:
            # Double-checked: another coroutine may have populated while we waited.
            cached = self._lookup(ref)
            if cached is not None:
                return cached

            details_list = await self._transport._resolve_via_ib(ref)

            if len(details_list) > 1:
                candidates = tuple(
                    _format_contract(d.contract, with_exchange=True)
                    for d in details_list[:_MAX_CANDIDATES]
                )
                raise AmbiguousContractError(
                    matches=details_list,
                    candidates=candidates,
                    message=(
                        f"reqContractDetails returned {len(details_list)} matches "
                        f"for {ref.symbol}; set primary_exchange or con_id to "
                        "disambiguate. Candidates: " + "; ".join(candidates)
                    ),
                )

            if details_list:
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
                        "contract_details": details,
                    },
                )

                if len(self._cache) >= _MAX_CACHE:
                    _, evicted = self._cache.popitem(last=False)  # FIFO eviction
                    self._drop_con_id(evicted)

                self._cache[ref] = resolved
                self._by_con_id[int(resolved.contract_id)] = resolved
                return resolved

        # Not found: the similar-symbol search (match_symbols, up to 3s) runs
        # outside the lock so it cannot block other resolves. Never cached.
        raise await self._not_found_error(ref)

    async def _not_found_error(self, ref: InstrumentRef) -> ContractNotFoundError:
        """Build a not-found error, suggesting similar symbols for symbol-based refs."""
        if ref.con_id is not None:
            return ContractNotFoundError(200, f"No security definition for conId={ref.con_id}")
        candidates: tuple[str, ...] = ()
        try:
            matches = await asyncio.wait_for(
                self._transport.match_symbols(ref.symbol), timeout=_MATCH_TIMEOUT_S
            )
            candidates = tuple(_format_contract(m.contract) for m in matches[:_MAX_CANDIDATES])
        except Exception:  # noqa: BLE001 - suggestions are best-effort
            candidates = ()
        hint = (
            "Did you mean: " + "; ".join(candidates)
            if candidates
            else "No similar symbols were found"
        )
        return ContractNotFoundError(
            200, f"No security definition for {ref.symbol}. {hint}", candidates=candidates
        )

    async def resolve_from_pair(self, pair: str) -> ResolvedContract:
        """Parse a 'SYMBOL-CURRENCY' pair string and delegate to resolve()."""
        ref = self._parse_pair(pair)
        return await self.resolve(ref)

    def invalidate(self, ref: InstrumentRef | None = None) -> None:
        """Remove a specific entry from the cache, or clear all entries."""
        if ref is None:
            self._cache.clear()
            self._by_con_id.clear()
        else:
            removed = self._cache.pop(ref, None)
            if removed is not None:
                self._by_con_id.pop(int(removed.contract_id), None)

    @staticmethod
    def _parse_pair(pair: str) -> InstrumentRef:
        """Parse 'AAPL-USD' → InstrumentRef(symbol='AAPL', STOCK, quote_currency='USD').

        Pair strings always resolve as STOCK. Callers needing other asset classes
        (crypto, forex, futures) must pass an InstrumentRef to the market-data and
        subscription APIs, which accept InstrumentRef overloads.
        """
        if "-" not in pair:
            return InstrumentRef(symbol=pair, instrument_type=InstrumentType.STOCK)
        symbol, _, quote = pair.partition("-")
        return InstrumentRef(
            symbol=symbol, instrument_type=InstrumentType.STOCK, quote_currency=quote
        )
