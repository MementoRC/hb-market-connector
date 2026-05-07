"""IbGatewayTransport: connection wrapper around ib_async.IB.

Stage 1 implements connect/disconnect/is_connected only. Request and
subscribe are stubbed with NotImplementedError pointing to the stage that
will implement them.

Conforms to RequestTransport and StreamTransport; held in the
unified_transport slot of the gateway since one socket carries both
request/response (placeOrder, reqContractDetails) and streaming
(reqMktData callbacks) traffic.

Stage 2 adds _handle_registry and _error_router; wires IB.errorEvent.
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
    IB = None  # noqa: F841

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
    from market_connector.orders import HBOrder


def _hb_to_ib_order(hb_order: HBOrder) -> Any:
    """Map an HBOrder to an ib_async Order. Stage 2 supports MARKET and LIMIT only.

    LIMIT_MAKER and any future conditional order types raise NotImplementedError —
    they land in Stage 4 when conditional order routing is implemented.

    Args:
        hb_order: Immutable order placement request with order_type, side,
            amount, and optional price.

    Returns:
        An ``ib_async.Order`` instance ready to pass to ``IB.placeOrder``.

    Raises:
        ValueError: When a LIMIT order is submitted without a price.
        NotImplementedError: When an order type beyond MARKET/LIMIT is requested.
    """
    from ib_async import Order  # noqa: PLC0415 — lazy, ib_async is optional

    from market_connector.orders import OrderType, TradeType  # noqa: PLC0415

    action = "BUY" if hb_order.side == TradeType.BUY else "SELL"

    if hb_order.order_type == OrderType.MARKET:
        return Order(
            action=action,
            orderType="MKT",
            totalQuantity=float(hb_order.amount),
        )

    if hb_order.order_type == OrderType.LIMIT:
        if hb_order.price is None:
            raise ValueError("LIMIT order requires non-None price")
        return Order(
            action=action,
            orderType="LMT",
            totalQuantity=float(hb_order.amount),
            lmtPrice=float(hb_order.price),
        )

    raise NotImplementedError(
        f"Order type {hb_order.order_type!r} not supported in Stage 2. "
        f"Conditional order types (LIMIT_MAKER, etc.) land in Stage 4."
    )


class IbGatewayTransport:
    """Connection wrapper around ib_async.IB.

    Stage 1 scope: connect/disconnect/is_connected.
    Stage 2 adds _handle_registry, _error_router, and wires errorEvent.
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
        self._handle_registry: dict[int, OrderHandle] = {}
        self._error_router = _ErrorRouter()

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
        self._ib.errorEvent += self._error_router.on_error

    async def disconnect(self) -> None:
        self._ib.errorEvent -= self._error_router.on_error
        self._ib.disconnect()

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "IbGatewayTransport does not implement request(); "
            "use typed methods on IbGatewayTransport directly"
        )

    async def _resolve_via_ib(self, ref: InstrumentRef) -> list:
        """Call ib_async.reqContractDetailsAsync with a Contract template built from ref.

        Returns the raw list of ContractDetails objects. Ambiguity checking and
        ResolvedContract construction live in IbContractResolver; this method is
        a thin I/O wrapper.
        """
        from ib_async import Contract  # noqa: PLC0415 — lazy, ib_async is optional

        from market_connector.exchanges.interactive_brokers.contract_resolver import (  # noqa: PLC0415
            _INSTRUMENT_TYPE_TO_SECTYPE,
        )

        template = Contract(
            symbol=ref.symbol,
            secType=_INSTRUMENT_TYPE_TO_SECTYPE.get(ref.instrument_type, "STK"),
            currency=ref.quote_currency or "USD",
            exchange=getattr(ref, "exchange_hint", None) or "SMART",
        )
        return await self._ib.reqContractDetailsAsync(template)  # type: ignore[no-any-return]

    def subscribe(
        self,
        channel: str,
        key: Any,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        raise NotImplementedError(
            "subscribe() is implemented in Stage 3 (reqMktData, reqMktDepth, etc.)"
        )
