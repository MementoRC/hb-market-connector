"""Protocol intersection types used as self-type annotations in IB mixins.

These allow mypy to type-check mixin method bodies that reference
self._transport, self._contract_resolver, and self._ready without requiring
a common concrete base class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@runtime_checkable
class HasIbTransport(Protocol):
    """Protocol for objects that expose an IbGatewayTransport as _transport."""

    _transport: IbGatewayTransport


@runtime_checkable
class HasContractResolver(Protocol):
    """Protocol for objects that expose an IbContractResolver as _contract_resolver."""

    _contract_resolver: IbContractResolver


@runtime_checkable
class HasReady(Protocol):
    """Protocol for objects that expose _ready flag and ensure_ready coroutine."""

    _ready: bool

    async def ensure_ready(self) -> None:
        """Raise if the gateway is not in a ready state."""
        ...
