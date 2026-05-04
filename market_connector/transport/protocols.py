"""Transport protocol family.

Three runtime-checkable Protocols form the transport contract:

- RequestTransport: request/response semantics (REST clients via request(), IB Gateway socket).
- StreamTransport: subscription/streaming semantics (WS clients via subscribe(), IB Gateway socket).
- Transport: intersection of both (unified transports like IB Gateway socket).

Lifecycle (connect/disconnect/is_connected) is intentionally NOT part of these
protocols because RestConnectorBase has no explicit connect/disconnect methods --
httpx manages the connection pool lazily; close() handles teardown. Lifecycle
is handled at the gateway layer via concrete transport references.

WsConnectorBase.subscribe() at transport/ws_base.py:113 has signature
    (channel: str, pair: str | None, handler: MessageCallback) -> None
which structurally conforms to StreamTransport.subscribe (key: Any is a
generalization of pair: str | None). The asynccontextmanager pattern lives at
the SubscriptionsMixin layer (exchanges/<x>/mixins/subscriptions.py via
@asynccontextmanager decorator), not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@runtime_checkable
class RequestTransport(Protocol):
    """Request/response transport semantics.

    REST clients (RestConnectorBase via request()) and IB Gateway socket
    conform. Lifecycle (connect/disconnect/is_connected) is NOT a protocol
    requirement: RestConnectorBase has no explicit connect/disconnect (httpx
    manages connection pool lazily; close() handles teardown). Lifecycle is
    handled at the gateway layer via concrete transport references.
    """

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class StreamTransport(Protocol):
    """Subscription/streaming transport semantics.

    Matches WsConnectorBase.subscribe() callback registration shape exactly.
    Teardown is transport-specific (not in this protocol). The
    asynccontextmanager pattern lives at the SubscriptionsMixin layer.
    """

    def subscribe(
        self,
        channel: str,
        key: Any,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None: ...


@runtime_checkable
class Transport(RequestTransport, StreamTransport, Protocol):
    """Unified transport: a single channel that carries BOTH request/response
    AND streaming traffic. IB Gateway TCP socket conforms (one socket carries
    both reqContractDetails responses and tickPrice events). Pure REST or
    pure WS transports do NOT conform.
    """

    pass
