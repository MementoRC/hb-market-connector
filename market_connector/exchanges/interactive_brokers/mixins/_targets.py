"""Shared target resolution for IB market-data and subscription mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.contracts.instrument import InstrumentRef

if TYPE_CHECKING:
    from market_connector.contracts.protocols import ResolvedContract
    from market_connector.exchanges.interactive_brokers.contract_resolver import (
        IbContractResolver,
    )
    from market_connector.primitives import ConnectorPair


async def resolve_target(
    target: InstrumentRef | ConnectorPair,
    resolver: IbContractResolver,
) -> tuple[ResolvedContract, str]:
    """Resolve target to (contract, label).

    InstrumentRef resolves via resolver.resolve(); label is "SYMBOL-QUOTE" when
    quote_currency is set, else the bare symbol. A pair string resolves as STOCK
    via resolve_from_pair() and is used as the label unchanged.
    """
    if isinstance(target, InstrumentRef):
        label = (
            f"{target.symbol}-{target.quote_currency}" if target.quote_currency else target.symbol
        )
        return await resolver.resolve(target), label
    return await resolver.resolve_from_pair(target), target
