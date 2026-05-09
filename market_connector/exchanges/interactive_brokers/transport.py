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

import asyncio
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
        self._first_status_timeout: float = 30.0

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

    async def place_order(self, native_contract: Any, hb_order: HBOrder) -> OrderHandle:
        """Submit an order to IB and await the first non-PendingSubmit status update.

        The caller (gateway) is responsible for pre-resolving the contract; this
        method receives the raw ib_async Contract object directly.

        Returns an OrderHandle snapshot reflecting the first meaningful status.
        Raises ConnectionLostError if not connected, OrderRejectedError if IB
        rejects the order before statusEvent fires, or asyncio.TimeoutError if
        no status arrives within _first_status_timeout seconds.
        """
        if not self._error_router.is_connected:
            from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
                ConnectionLostError,
            )

            raise ConnectionLostError(1100, "transport not connected")

        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
        )

        ib_order = _hb_to_ib_order(hb_order)
        trade = self._ib.placeOrder(native_contract, ib_order)
        initial = OrderHandle.from_trade(trade)
        self._handle_registry[trade.order.orderId] = initial

        new_handle = await self._wait_first_status_update(trade)
        self._handle_registry[trade.order.orderId] = new_handle
        return new_handle

    async def _wait_first_status_update(self, trade: Any) -> OrderHandle:
        """Await the first meaningful status update for a submitted trade.

        Either statusEvent resolves the future (success path) or the error router
        sets an exception on it (error-before-status race). The try/finally block
        guarantees cleanup of both the statusEvent hook and the pending waiter entry.
        """
        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
        )

        fut: asyncio.Future[OrderHandle] = asyncio.get_running_loop().create_future()
        self._error_router._pending_order_waiters[trade.order.orderId] = fut

        def _on_status(*_args: Any) -> None:
            try:
                new_handle = OrderHandle.from_trade(trade)
            except ValueError:
                return
            # Ignore the initial PendingSubmit echo — wait for a real status.
            if new_handle.raw_status == "PendingSubmit":
                return
            if not fut.done():
                fut.set_result(new_handle)

        trade.statusEvent += _on_status
        try:
            return await asyncio.wait_for(fut, timeout=self._first_status_timeout)
        finally:
            trade.statusEvent -= _on_status
            self._error_router._pending_order_waiters.pop(trade.order.orderId, None)

    async def cancel_order(self, handle: OrderHandle) -> OrderHandle:
        """Cancel an active order and await the first non-PendingCancel status update.

        Idempotent on terminal orders (FILLED, CANCELLED, REJECTED) — returns the
        same handle without calling IB. Active orders call ib.cancelOrder and await
        statusEvent until a non-PendingCancel status is received.

        Args:
            handle: The OrderHandle returned by place_order.

        Returns:
            The updated OrderHandle reflecting the post-cancel status.

        Raises:
            asyncio.TimeoutError: If no decisive status arrives within 10 seconds.
        """
        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
            OrderState,
        )

        terminals = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
        if handle.status in terminals:
            return handle

        self._ib.cancelOrder(handle._trade.order)

        fut: asyncio.Future[OrderHandle] = asyncio.get_running_loop().create_future()

        def _on_status(*_args: Any) -> None:
            try:
                new_handle = OrderHandle.from_trade(handle._trade)
            except ValueError:
                return
            # Ignore intermediate PendingCancel — wait for a decisive status.
            if new_handle.raw_status == "PendingCancel":
                return
            if not fut.done():
                fut.set_result(new_handle)

        handle._trade.statusEvent += _on_status
        try:
            new_handle = await asyncio.wait_for(fut, timeout=10.0)
        finally:
            handle._trade.statusEvent -= _on_status

        self._handle_registry[handle._trade.order.orderId] = new_handle
        return new_handle

    def open_orders(self) -> list[OrderHandle]:
        """Return a snapshot of currently-open orders from the ib_async local cache.

        This method is synchronous — ib_async maintains a local in-memory cache of
        active trades; no network round-trip occurs. For each trade returned by
        openTrades(), the handle registry is updated so that callers who hold
        references from place_order() continue to have consistent order_ids.

        Raises ConnectionLostError if the transport is not connected.
        """
        if not self._error_router.is_connected:
            from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
                ConnectionLostError,
            )

            raise ConnectionLostError(1100, "transport not connected")

        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
        )

        trades = self._ib.openTrades()
        result: list[OrderHandle] = []
        for trade in trades:
            order_id = trade.order.orderId
            # Reconstruct to get current status snapshot; registry slot is preserved.
            handle = OrderHandle.from_trade(trade)
            self._handle_registry[order_id] = handle
            result.append(handle)
        return result

    def subscribe(
        self,
        channel: str,
        key: Any,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        raise NotImplementedError(
            "subscribe() is implemented in Stage 3 (reqMktData, reqMktDepth, etc.)"
        )
