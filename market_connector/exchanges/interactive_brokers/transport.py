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
import contextlib
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ib_async import IB
else:
    # ib_async is an optional dependency; the runtime import is guarded so the
    # module can be imported without the package installed when running tests
    # that don't actually instantiate the transport. Tests patch IB at the
    # module level.
    try:
        from ib_async import IB
    except ImportError:  # pragma: no cover - optional dep
        IB = None  # noqa: F841

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError
from market_connector.hb_compat.event_bus import EventBus
from market_connector.hb_compat.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from ib_async import Contract, Ticker

    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
    from market_connector.orders import HBOrder, TradeType

logger = get_logger(__name__)

# EventBus topic carrying ConnectionStateEvent payloads.
CONNECTION_TOPIC = "connection"


@dataclass(frozen=True)
class ConnectionStateEvent:
    """Connection lifecycle notification published on CONNECTION_TOPIC.

    ``state`` is one of "connected", "disconnected", "reconnecting" or
    "data_lost". ``code`` carries the IB error code when one triggered it.
    """

    state: str
    code: int | None = None
    message: str = ""


# EventBus topic carrying FarmStatusEvent payloads.
FARM_TOPIC = "farm"


@dataclass(frozen=True)
class FarmStatusEvent:
    """Data-farm status notification published on FARM_TOPIC.

    ``farm`` is "market", "hmds" or "secdef"; ``ok`` is False for the
    broken codes (2103/2105/2157) and True for the OK codes (2104/2106/2158).
    """

    farm: str
    ok: bool
    code: int
    message: str = ""


# EventBus topics carrying FillEvent / FeeEvent payloads.
FILL_TOPIC = "fill"
FEE_TOPIC = "fee"

# Upper bound on remembered execution ids (fills and fees each); oldest evicted first.
_MAX_TRACKED_EXECUTIONS = 10_000


@dataclass(frozen=True)
class FillEvent:
    """One execution of an order, published on FILL_TOPIC as soon as IB reports it.

    Fills are never buffered waiting for their commission: the fee follows as
    a separate :class:`FeeEvent` (same ``exec_id``) when IB sends the report,
    which may be much later. ``is_correction`` marks an execution IB re-sent
    with the same id prefix but a new final ``.NN`` segment (a trade bust or
    correction). ``reconciled`` marks fills recovered by the post-(re)connect
    executions query instead of the live stream.
    """

    exec_id: str
    order_id: int
    perm_id: int
    con_id: int
    symbol: str
    side: TradeType
    price: Decimal
    size: Decimal
    cum_qty: Decimal
    time: datetime
    account: str
    is_correction: bool = False
    reconciled: bool = False


@dataclass(frozen=True)
class FeeEvent:
    """Fee for an execution, published on FEE_TOPIC when IB's report arrives.

    Deliberately named "fee" rather than "commission": IB renamed its callback
    to ``commissionAndFeesReport``. Always published after (or independently
    of) the matching :class:`FillEvent`; deduplicated by full ``exec_id``.
    """

    exec_id: str
    order_id: int
    fee: Decimal
    fee_currency: str
    reconciled: bool = False


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


@dataclass
class SubscriptionHandle:
    """Handle returned by IbGatewayTransport.subscribe(). Owns cleanup on close()."""

    channel: str
    contract: object
    _cleanup: Callable[[], None]
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        """Unregister event handler and cancel IB stream. Idempotent."""
        if not self._closed:
            self._closed = True
            self._cleanup()


@dataclass
class _ActiveSubscription:
    """Registry entry able to re-issue its IB request and re-hook the new ticker."""

    event_key: str
    channel: str
    contract: Any
    dispatch: Callable[[Any], None]
    ticker: Any = None


class IbGatewayTransport:
    """Connection wrapper around ib_async.IB.

    Stage 1 scope: connect/disconnect/is_connected.
    Stage 2 adds _handle_registry, _error_router, and wires errorEvent.
    Stage 3 adds subscribe() (reqMktData / reqMktDepth event routing), fanned
    out through a canonical :class:`event_bus.EventBus` instance instead of a
    bespoke closure-based dispatch (ADR 0001 Group D, issue #44). ib_async's
    own Event objects (``ticker.updateEvent`` etc.) remain the upstream
    trigger — that part is ib_async's API surface, not ours to replace — but
    the fan-out to the caller's callback is bridged through the bus.
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
        self._error_router = _ErrorRouter(market_data_type=spec.resolved_market_data_type)
        self._first_status_timeout: float = 30.0
        self._event_bus = EventBus(name="ib-ws")
        self._subscribe_seq: int = 0
        self._subscriptions: dict[str, _ActiveSubscription] = {}
        # Fill de-duplication: execId base (final ".NN" removed) -> last full execId.
        self._seen_fills: OrderedDict[str, str] = OrderedDict()
        self._seen_fees: OrderedDict[str, None] = OrderedDict()
        # Reconnect supervisor state.
        self._handlers_attached: bool = False
        self._closing: bool = False
        self._supervisor: asyncio.Task[None] | None = None
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # patchable in tests
        self._error_router._connection_listeners.append(self._on_router_connection)
        self._error_router._data_lost_listeners.append(self._on_data_lost)

    @property
    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    async def connect(self) -> None:
        """Connect to IB Gateway. Auth is handled by Gateway externally
        (interactive 2FA login at Gateway startup); this just opens the socket.

        Raises whatever ``connectAsync`` raises; on failure no handlers stay
        attached and no state is published.
        """
        self._closing = False
        await self._connect_once()
        self._publish_state("connected")

    async def _connect_once(self) -> None:
        """Run the connect sequence (used by connect() and the supervisor).

        Handlers are attached only after connectAsync returns, and only once
        across reconnects, so a failed attempt leaves nothing behind.
        """
        if self._spec.pace_api:
            self._ib.client.setConnectOptions("+PACEAPI")
        await self._ib.connectAsync(
            host=self._spec.host,
            port=self._spec.port,
            clientId=self._spec.client_id,
            readonly=self._spec.readonly,
            account=self._spec.account_id or "",
            raiseSyncErrors=True,
        )
        try:
            self._attach_handlers()
            self._apply_market_data_type()
            await self._reconcile_executions()
        except BaseException:
            self._detach_handlers()
            raise
        self._error_router.set_connected(True)

    def _attach_handlers(self) -> None:
        if self._handlers_attached:
            return
        self._ib.errorEvent += self._error_router.on_error
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.commissionReportEvent += self._on_commission_report
        self._handlers_attached = True

    def _detach_handlers(self) -> None:
        if not self._handlers_attached:
            return
        self._ib.errorEvent -= self._error_router.on_error
        self._ib.disconnectedEvent -= self._on_disconnected
        self._ib.execDetailsEvent -= self._on_exec_details
        self._ib.commissionReportEvent -= self._on_commission_report
        self._handlers_attached = False

    @staticmethod
    def _remember(seen: OrderedDict[str, Any], key: str, value: Any) -> None:
        """Record *key* as the newest entry, evicting the oldest beyond the cap."""
        seen[key] = value
        seen.move_to_end(key)
        while len(seen) > _MAX_TRACKED_EXECUTIONS:
            seen.popitem(last=False)

    def _on_exec_details(self, _trade: Any, fill: Any) -> None:
        """IB.execDetailsEvent handler: publish the live fill immediately."""
        self._ingest_fill(fill, reconciled=False)

    def _on_commission_report(self, _trade: Any, fill: Any, report: Any) -> None:
        """IB.commissionReportEvent handler: publish the fee when it arrives."""
        self._ingest_fee(fill, report, reconciled=False)

    @staticmethod
    def _exec_suffix(exec_id: str) -> int:
        """Parse the final dot-segment of exec_id as an int; return -1 if absent or non-numeric."""
        _, _, suffix = exec_id.rpartition(".")
        if not suffix:
            return -1
        try:
            return int(suffix)
        except ValueError:
            return -1

    def _ingest_fill(self, fill: Any, *, reconciled: bool) -> bool:
        """Publish *fill* unless already seen; returns True when published.

        IB re-sends executions. The identical full execId is dropped; the same
        base with a different ``.NN`` suffix is a correction and is published
        with ``is_correction=True``, but only if the suffix is strictly newer.
        """
        from market_connector.orders import TradeType  # noqa: PLC0415

        ex = fill.execution
        exec_id: str = ex.execId
        base = exec_id.rpartition(".")[0] or exec_id
        last = self._seen_fills.get(base)
        if last is not None and self._exec_suffix(exec_id) <= self._exec_suffix(last):
            return False
        self._remember(self._seen_fills, base, exec_id)
        self._event_bus.publish(
            FILL_TOPIC,
            FillEvent(
                exec_id=exec_id,
                order_id=ex.orderId,
                perm_id=ex.permId,
                con_id=fill.contract.conId,
                symbol=fill.contract.symbol,
                side=TradeType.BUY if ex.side == "BOT" else TradeType.SELL,
                price=Decimal(str(ex.price)),
                size=Decimal(str(ex.shares)),
                cum_qty=Decimal(str(ex.cumQty)),
                time=fill.time,
                account=ex.acctNumber,
                is_correction=last is not None,
                reconciled=reconciled,
            ),
        )
        return True

    def _ingest_fee(self, fill: Any, report: Any, *, reconciled: bool) -> bool:
        """Publish the fee in *report* unless its execId is empty or already seen."""
        exec_id: str = report.execId
        if not exec_id or exec_id in self._seen_fees:
            return False
        self._remember(self._seen_fees, exec_id, None)
        self._event_bus.publish(
            FEE_TOPIC,
            FeeEvent(
                exec_id=exec_id,
                order_id=fill.execution.orderId,
                fee=Decimal(str(report.commission)),
                fee_currency=report.currency,
                reconciled=reconciled,
            ),
        )
        return True

    async def _reconcile_executions(self) -> None:
        """Recover executions missed while disconnected, via reqExecutionsAsync.

        Runs after every successful (re)connect. Fills and fees go through the
        same de-duplication as the live stream, flagged ``reconciled=True``,
        so anything already seen live is not re-emitted. Failure is logged and
        never fails the connect.
        """
        try:
            from ib_async import ExecutionFilter  # noqa: PLC0415 — lazy, ib_async is optional

            fills = await self._ib.reqExecutionsAsync(
                ExecutionFilter(clientId=self._spec.client_id, acctCode=self._spec.account_id or "")
            )
            for fill in fills:
                self._ingest_fill(fill, reconciled=True)
                report = fill.commissionReport
                if report is not None:
                    self._ingest_fee(fill, report, reconciled=True)
        except Exception as exc:
            logger.warning("IB execution reconcile failed: %s", exc)

    def _publish_state(self, state: str, code: int | None = None, message: str = "") -> None:
        """Publish a ConnectionStateEvent on the connection topic."""
        self._event_bus.publish(CONNECTION_TOPIC, ConnectionStateEvent(state, code, message))

    def _on_router_connection(self, is_up: bool) -> None:
        """Mirror IB-originated 1100/1101/1102 connectivity codes onto the bus."""
        self._publish_state("connected" if is_up else "disconnected")

    def _on_data_lost(self, code: int, msg: str) -> None:
        """1101: socket is fine but IB dropped our streams — re-request them."""
        self._publish_state("data_lost", code, msg)
        self.resubscribe_all(cancel_old=True)

    def _on_disconnected(self, *_args: Any) -> None:
        """disconnectedEvent handler: start the reconnect supervisor unless we caused it."""
        if self._closing:
            return
        if self._supervisor is not None and not self._supervisor.done():
            return  # failed attempts also fire this; never run two supervisors
        self._error_router.set_connected(False)
        self._error_router._fail_all_pending(ConnectionLostError(1100, "socket disconnected"))
        self._publish_state("disconnected")
        self._supervisor = asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff until success or cancellation."""
        delay = self._spec.reconnect_initial_delay
        while True:
            await self._sleep(delay)
            self._publish_state("reconnecting")
            try:
                await self._connect_once()
            except Exception as exc:
                logger.warning("IB reconnect attempt failed (next delay %.1fs): %s", delay, exc)
                delay = min(delay * 2, self._spec.reconnect_max_delay)
                continue
            break
        # A full reconnect drops every old stream server-side: re-request, no cancel.
        self.resubscribe_all(cancel_old=False)
        self._publish_state("connected")

    def _apply_market_data_type(self) -> None:
        """Send the configured market data type to IB.

        Must run after every successful (re)connect, since IB resets it per
        session; a reconnect supervisor should call this too.
        """
        self._ib.reqMarketDataType(self._spec.resolved_market_data_type)

    async def disconnect(self) -> None:
        """Stop the supervisor, detach handlers and close the socket."""
        self._closing = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        self._detach_handlers()
        self._error_router.set_connected(False)
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
        return await self._ib.reqContractDetailsAsync(template)

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
            IBOrderState,
            OrderHandle,
        )

        terminals = {IBOrderState.FILLED, IBOrderState.CANCELLED, IBOrderState.REJECTED}
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
        contract: Contract,
        callback: Callable[[Any], None],
    ) -> SubscriptionHandle:
        """Register a synchronous callback for IB event-driven market data.

        The callback is registered on a canonical :class:`event_bus.EventBus`
        instance (ADR 0001 Group D, issue #44); ib_async's native
        ``ticker.updateEvent`` remains the upstream trigger (ib_async's own
        API — not reinvented here) and is bridged into publish() calls per
        subscription (one per entry of ``ticker.tickByTicks`` for trades). EventBus.publish
        isolates and logs handler exceptions, replacing the previous
        try/except-around-callback pattern.

        Args:
            channel: "depth" (DOM updates via reqMktDepth) or "trades"
                (tick-by-tick via reqTickByTickData).
            callback: sync Callable[[ticker_or_tick], None].

        Returns:
            SubscriptionHandle whose .close() unregisters the handler and
            cancels the IB stream.
        """
        if channel not in ("depth", "trades"):
            raise ValueError(f"Unknown subscribe channel {channel!r}; expected 'depth' or 'trades'")

        self._subscribe_seq += 1
        event_key = f"{channel}:{self._subscribe_seq}"
        bus_subscription = self._event_bus.subscribe(event_key, callback)

        def _dispatch_depth(t: object) -> None:
            self._event_bus.publish(event_key, t)

        def _dispatch_trades(t: Ticker) -> None:
            # ib_async appends each batch to ticker.tickByTicks, then fires updateEvent.
            for tick in list(t.tickByTicks):
                self._event_bus.publish(event_key, tick)

        active = _ActiveSubscription(
            event_key=event_key,
            channel=channel,
            contract=contract,
            dispatch=_dispatch_depth if channel == "depth" else _dispatch_trades,
        )
        self._issue_stream(active)
        self._subscriptions[event_key] = active

        def _cleanup() -> None:
            self._subscriptions.pop(event_key, None)
            self._unhook_stream(active)
            self._event_bus.unsubscribe(bus_subscription)
            self._cancel_stream(active)

        return SubscriptionHandle(channel=channel, contract=contract, _cleanup=_cleanup)

    def _issue_stream(self, sub: _ActiveSubscription) -> None:
        """Send the IB request for *sub* and hook the resulting ticker's updateEvent."""
        if sub.channel == "depth":
            ticker = self._ib.reqMktDepth(sub.contract, numRows=10, isSmartDepth=True)
        else:
            ticker = self._ib.reqTickByTickData(sub.contract, "AllLast", 0, False)
        ticker.updateEvent += sub.dispatch
        sub.ticker = ticker

    def _unhook_stream(self, sub: _ActiveSubscription) -> None:
        """Detach *sub*'s dispatcher from its current ticker, if hooked."""
        if sub.ticker is not None:
            sub.ticker.updateEvent -= sub.dispatch
            sub.ticker = None

    def _cancel_stream(self, sub: _ActiveSubscription) -> None:
        """Cancel the IB-side stream for *sub*."""
        if sub.channel == "depth":
            self._ib.cancelMktDepth(sub.contract, isSmartDepth=True)
        else:
            self._ib.cancelTickByTickData(sub.contract, "AllLast")

    def resubscribe_all(self, *, cancel_old: bool = True) -> None:
        """Re-issue every active subscription's IB request on a fresh ticker.

        The bus subscription and the caller's callback are untouched; only the
        ticker hook moves. Pass ``cancel_old=False`` after a full reconnect,
        where the old streams no longer exist server-side.
        """
        for sub in list(self._subscriptions.values()):
            try:
                self._unhook_stream(sub)
                if cancel_old:
                    self._cancel_stream(sub)
                self._issue_stream(sub)
            except Exception as exc:
                logger.warning("IB resubscribe of %s failed: %s", sub.event_key, exc)

    def subscribe_connection_state(
        self, callback: Callable[[ConnectionStateEvent], None]
    ) -> SubscriptionHandle:
        """Observe connection state changes; the handle's close() unsubscribes."""
        bus_subscription = self._event_bus.subscribe(CONNECTION_TOPIC, callback)
        return SubscriptionHandle(
            channel=CONNECTION_TOPIC,
            contract=None,
            _cleanup=lambda: self._event_bus.unsubscribe(bus_subscription),
        )

    @property
    def stale_farms(self) -> frozenset[str]:
        """Names of data farms currently reported broken ("market", "hmds", "secdef")."""
        raise NotImplementedError

    def subscribe_farm_status(
        self, callback: Callable[[FarmStatusEvent], None]
    ) -> SubscriptionHandle:
        """Observe data-farm status changes; the handle's close() unsubscribes."""
        raise NotImplementedError

    def cancel_reason(self, order_id: int) -> str | None:
        """Return IB's code-202 cancellation text for *order_id*, or None if unknown."""
        raise NotImplementedError

    def subscribe_fills(self, callback: Callable[[FillEvent], None]) -> SubscriptionHandle:
        """Observe executions (live and reconciled); the handle's close() unsubscribes."""
        bus_subscription = self._event_bus.subscribe(FILL_TOPIC, callback)
        return SubscriptionHandle(
            channel=FILL_TOPIC,
            contract=None,
            _cleanup=lambda: self._event_bus.unsubscribe(bus_subscription),
        )

    def subscribe_fees(self, callback: Callable[[FeeEvent], None]) -> SubscriptionHandle:
        """Observe execution fees (live and reconciled); the handle's close() unsubscribes."""
        bus_subscription = self._event_bus.subscribe(FEE_TOPIC, callback)
        return SubscriptionHandle(
            channel=FEE_TOPIC,
            contract=None,
            _cleanup=lambda: self._event_bus.unsubscribe(bus_subscription),
        )
