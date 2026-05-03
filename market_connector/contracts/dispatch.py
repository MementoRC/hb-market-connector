"""native_for() helper: resolve a target to its native exchange identifier.

Picks ContractResolver when present (TransportAwareGateway with non-None
contract_resolver); falls back to SymbolMapper otherwise. Raises on any
combination that cannot resolve the target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from market_connector.contracts.instrument import InstrumentRef

if TYPE_CHECKING:
    from market_connector.primitives import ConnectorPair


async def native_for(
    gateway: Any,
    target: ConnectorPair | InstrumentRef,
) -> Any:
    """Resolve target -> native exchange identifier.

    Picks ContractResolver if the gateway exposes a non-None contract_resolver;
    falls back to SymbolMapper if symbol_mapper is set; raises otherwise.

    ConnectorPair is a str type alias, so dispatch is via isinstance(InstrumentRef)
    rather than isinstance(ConnectorPair).
    """
    resolver = getattr(gateway, "contract_resolver", None)
    if resolver is not None:
        if isinstance(target, InstrumentRef):
            return (await resolver.resolve(target)).native
        return (await resolver.resolve_from_pair(target)).native

    mapper = getattr(gateway, "symbol_mapper", None)
    if mapper is None:
        raise ValueError(
            f"{type(gateway).__name__} has neither contract_resolver nor "
            f"symbol_mapper; cannot resolve {target!r}"
        )
    if isinstance(target, InstrumentRef):
        raise ValueError(
            f"{type(gateway).__name__} cannot resolve InstrumentRef (no ContractResolver)"
        )
    return mapper.map(target)
