"""IbGatewayTransport: connection wrapper around ib_async.IB.

Stage 1 implements connect/disconnect/is_connected only. Request and
subscribe are stubbed with NotImplementedError pointing to the stage that
will implement them.

Conforms to RequestTransport and StreamTransport; held in the
unified_transport slot of the gateway since one socket carries both
request/response (placeOrder, reqContractDetails) and streaming
(reqMktData callbacks) traffic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# ib_async is an optional dependency; import is inside __init__ so that the
# module can be imported without the package installed when running tests
# that don't actually instantiate the transport. Tests patch IB at the
# module level.
try:
    from ib_async import IB  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dep
    IB = None

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec


class IbGatewayTransport:
    """Connection wrapper around ib_async.IB.

    Stage 1 scope: connect/disconnect/is_connected.
    Stage 2 will add request() (reqContractDetails et al via reqId tracking).
    Stage 3 will add subscribe() (reqMktData / reqMktDepth event routing).
    """

    def __init__(self, spec: IbConnectionSpec) -> None:
        if IB is None:
            raise ImportError(
                "ib_async is required for IbGatewayTransport. "
                "Install with: pixi add --feature interactive-brokers ib_async"
            )
        self._spec = spec
        self._ib = IB()

    @property
    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    async def connect(self) -> None:
        """Connect to IB Gateway. Auth is handled by Gateway externally
        (interactive 2FA login at Gateway startup); this just opens the socket."""
        await self._ib.connectAsync(
            host=self._spec.host,
            port=self._spec.port,
            clientId=self._spec.client_id,
        )

    async def disconnect(self) -> None:
        self._ib.disconnect()

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "request() is implemented in Stage 2 (reqContractDetails, placeOrder, etc.)"
        )

    def subscribe(
        self,
        channel: str,
        key: Any,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        raise NotImplementedError(
            "subscribe() is implemented in Stage 3 (reqMktData, reqMktDepth, etc.)"
        )
