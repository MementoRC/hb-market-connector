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
import uuid
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

from market_connector.contracts.protocols import ResolvedContract
from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    IbAccountConfigError,
    IbReadOnlyApiError,
    IbReadOnlyModeError,
    IbVersionIncompatibleError,
    LinkedOrderRejectedError,
    MarketDataLinesExhaustedError,
    MarketDataPermissionError,
    MarketDataTimeoutError,
    OrderRejectedError,
    PnlTimeoutError,
    WhatIfTimeoutError,
)
from market_connector.exchanges.interactive_brokers.linked_orders import OcaType
from market_connector.exchanges.interactive_brokers.pacing import IbPacer
from market_connector.hb_compat.event_bus import EventBus
from market_connector.hb_compat.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable, Sequence
    from datetime import datetime

    from ib_async import Contract, Ticker

    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.exchanges.interactive_brokers.linked_orders import IbBracket, IbOcaGroup
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.order_options import IbOrderOptions
    from market_connector.exchanges.interactive_brokers.pnl import IbPnL, IbPositionPnL
    from market_connector.exchanges.interactive_brokers.positions import IbPosition
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
    from market_connector.exchanges.interactive_brokers.what_if import IbWhatIfResult
    from market_connector.orders import HBOrder, TradeType

logger = get_logger(__name__)

# EventBus topic carrying ConnectionStateEvent payloads.
CONNECTION_TOPIC = "connection"


def _stream_key(contract: Any) -> Hashable:
    """Registry key for a contract: its stable IB id, not the (possibly unhashable) object."""
    contract_id: Hashable | None = getattr(contract, "contract_id", None)
    return contract_id if contract_id else contract


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """True for a connect timeout: builtin TimeoutError or ib_async's sync-request failure."""
    if isinstance(exc, TimeoutError):
        return True
    return type(exc) is ConnectionError and bool(exc.args) and isinstance(exc.args[0], list)


def _is_order_sync_timeout(exc: BaseException) -> bool:
    """True for ib_async's startup order-sync timeout (what READ_ONLY_API=yes causes)."""
    if type(exc) is not ConnectionError or not exc.args:
        return False
    first = exc.args[0]
    messages = first if isinstance(first, list) else exc.args
    return any(
        isinstance(m, str)
        and ("open orders request timed out" in m or "completed orders request timed out" in m)
        for m in messages
    )


def _native(contract: Any) -> Any:
    """Unwrap a ResolvedContract to the ib_async Contract IB calls expect."""
    return contract.native if isinstance(contract, ResolvedContract) else contract


class _WireDecimal(Decimal):
    """Decimal whose str() is positional, as IB's wire format needs.

    ib_async formats order Decimals via str(); stock Decimal emits '1E+2' / '1E-7'.
    """

    __slots__ = ()

    def __str__(self) -> str:
        return format(self, "f")


def _exact(value: Decimal) -> Any:
    """An order field as the exact Decimal (no float rounding).

    ib_async 2.1 annotates Order.totalQuantity as float but formats any value with str() on
    the wire; 3.0 types it Decimal. Typed Any so one call serves both stubs.
    """
    return _WireDecimal(value)


def _issue_request(ib: Any, kind: str, request: Callable[[], Ticker]) -> tuple[Ticker, int | None]:
    """Run an ib_async ticker *request*; return its ticker and the reqId sent on the wire.

    ib_async 3.0 removed ``Wrapper.ticker2ReqId`` (2.x) for a private subscription registry,
    but both draw the reqId from ``Client.getReqId()``, so that call is observed. *kind* is
    the 2.x registry key ("mktData", "mktDepth", "tickByTick"), consulted only when the
    request drew no id. None when neither source knows the id.
    """
    client = ib.client
    real = client.getReqId
    issued: list[int] = []

    def _spy() -> int:
        req_id: int = real()
        issued.append(req_id)
        return req_id

    client.getReqId = _spy
    try:
        ticker = request()
    finally:
        client.getReqId = real
    if issued:
        return ticker, issued[0]
    registry = getattr(ib.wrapper, "ticker2ReqId", None)
    return ticker, registry[kind].get(ticker) if registry is not None else None


@dataclass(frozen=True)
class ConnectionStateEvent:
    """Connection lifecycle notification published on CONNECTION_TOPIC.

    ``state`` is one of "connected", "disconnected", "reconnecting" or
    "data_lost". ``code`` carries the IB error code when one triggered it.
    """

    state: str
    code: int | None = None
    message: str = ""


@dataclass(frozen=True)
class IbServerInfo:
    """Server version negotiated at connect, against ib_async's client ceiling.

    ``max_client_version`` is None when ib_async does not expose it.
    ``at_client_ceiling`` means the server may offer features the client
    library cannot yet speak.
    """

    server_version: int
    max_client_version: int | None

    @property
    def at_client_ceiling(self) -> bool:
        ceiling = self.max_client_version
        return ceiling is not None and self.server_version >= ceiling


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

# Depth rows requested per side (reqMktDepth numRows); the own book is truncated to this.
_DEPTH_ROWS = 10

# Market-data permission codes reported against a persistent stream's reqId (issue #75).
# HARD: the stream can never produce data. SOFT: IB may still deliver partial data, so
# only a waiter that times out fails (with the remembered code and text).
_HARD_PERMISSION_CODES = frozenset({354})
_SOFT_PERMISSION_CODES = frozenset({2152, 10089, 10090})

# IB error 101: the account's market-data line allowance is used up (issue #104).
_MAX_TICKERS_CODE = 101


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


def _apply_order_options(
    order: Any, native_contract: Any, hb_order: HBOrder, options: IbOrderOptions | None
) -> None:
    """Apply *options* to the converted IB *order* in place; no-op when none are set.

    Adaptive requires a SMART-routed STK contract and a MARKET or LIMIT order.

    Raises:
        ValueError: the options do not fit the contract or order type.
    """
    if options is None or options.adaptive_priority is None:
        return

    from ib_async import TagValue  # noqa: PLC0415 — lazy, ib_async is optional

    from market_connector.orders import OrderType  # noqa: PLC0415

    if native_contract.secType != "STK":
        raise ValueError(f"Adaptive algo requires secType STK, got {native_contract.secType!r}")
    if native_contract.exchange != "SMART":
        raise ValueError(f"Adaptive algo requires exchange SMART, got {native_contract.exchange!r}")
    if hb_order.order_type not in (OrderType.MARKET, OrderType.LIMIT):
        raise ValueError(
            f"Adaptive algo supports only MARKET and LIMIT orders, got {hb_order.order_type.name}"
        )
    order.algoStrategy = "Adaptive"
    order.algoParams = [TagValue("adaptivePriority", options.adaptive_priority.value)]


def _hb_to_ib_order(hb_order: HBOrder) -> Any:
    """Map an HBOrder to an ib_async Order.

    Supported types:
        MARKET: ``MKT`` order.
        LIMIT: ``LMT`` order at ``price``.
        STOP_LOSS: ``STP`` order triggered at ``stop_price`` (``auxPrice``).
        STOP_LOSS_LIMIT: ``STP LMT`` order triggered at ``stop_price`` (``auxPrice``)
            and placed at ``price`` (``lmtPrice``).

    ``stop_price`` is only valid for STOP_LOSS/STOP_LOSS_LIMIT. LIMIT_MAKER (post-only)
    is rejected because IB has no post-only equivalent. Other types are unsupported.

    Args:
        hb_order: Immutable order placement request with order_type, side,
            amount, optional price, optional stop_price, time_in_force (-> ``tif``)
            and outside_rth (-> ``outsideRth``, set only when True).

    Returns:
        An ``ib_async.Order`` instance ready to pass to ``IB.placeOrder``.

    Raises:
        ValueError: When a required price/stop_price is missing, or a stop_price is
            supplied for a non-stop order type.
        NotImplementedError: For LIMIT_MAKER or any other unsupported order type.
    """
    from ib_async import Order  # noqa: PLC0415 — lazy, ib_async is optional

    from market_connector.orders import OrderType, TradeType  # noqa: PLC0415

    action = "BUY" if hb_order.side == TradeType.BUY else "SELL"

    if hb_order.stop_price is not None and hb_order.order_type in (
        OrderType.MARKET,
        OrderType.LIMIT,
    ):
        raise ValueError(
            "stop_price is only valid for STOP_LOSS/STOP_LOSS_LIMIT orders, "
            f"not {hb_order.order_type}"
        )

    if hb_order.order_type == OrderType.MARKET:
        market = Order(action=action, orderType="MKT", totalQuantity=_exact(hb_order.amount))
        # Explicit DAY: without it IB answers 10349 and ib_async locally cancels the trade.
        market.tif = str(hb_order.time_in_force)
        if hb_order.outside_rth:
            market.outsideRth = True
        return market

    if hb_order.order_type == OrderType.LIMIT:
        if hb_order.price is None:
            raise ValueError("LIMIT order requires non-None price")
        limit = Order(
            action=action,
            orderType="LMT",
            totalQuantity=_exact(hb_order.amount),
            lmtPrice=_exact(hb_order.price),
        )
        limit.tif = str(hb_order.time_in_force)
        if hb_order.outside_rth:
            limit.outsideRth = True
        return limit

    if hb_order.order_type == OrderType.STOP_LOSS:
        if hb_order.stop_price is None:
            raise ValueError("STOP_LOSS order requires non-None stop_price")
        stop = Order(
            action=action,
            orderType="STP",
            totalQuantity=_exact(hb_order.amount),
            auxPrice=_exact(hb_order.stop_price),
        )
        stop.tif = str(hb_order.time_in_force)
        if hb_order.outside_rth:
            stop.outsideRth = True
        return stop

    if hb_order.order_type == OrderType.STOP_LOSS_LIMIT:
        if hb_order.stop_price is None:
            raise ValueError("STOP_LOSS_LIMIT order requires non-None stop_price")
        if hb_order.price is None:
            raise ValueError("STOP_LOSS_LIMIT order requires non-None price")
        stop_limit = Order(
            action=action,
            orderType="STP LMT",
            totalQuantity=_exact(hb_order.amount),
            lmtPrice=_exact(hb_order.price),
            auxPrice=_exact(hb_order.stop_price),
        )
        stop_limit.tif = str(hb_order.time_in_force)
        if hb_order.outside_rth:
            stop_limit.outsideRth = True
        return stop_limit

    if hb_order.order_type == OrderType.LIMIT_MAKER:
        raise NotImplementedError(
            "LIMIT_MAKER (post-only) is not supported by Interactive Brokers; "
            "place a LIMIT order instead."
        )

    raise NotImplementedError(
        f"Order type {hb_order.order_type!r} is not supported by the IB transport."
    )


def _validate_bracket(entry: HBOrder, take_profit: HBOrder, stop_loss: HBOrder) -> None:
    """Reject a malformed bracket with ValueError before anything reaches IB."""
    from market_connector.orders import OrderType  # noqa: PLC0415

    if entry.order_type not in (OrderType.MARKET, OrderType.LIMIT):
        raise ValueError(f"bracket entry must be MARKET or LIMIT, not {entry.order_type}")
    if take_profit.order_type != OrderType.LIMIT:
        raise ValueError(f"bracket take_profit must be LIMIT, not {take_profit.order_type}")
    if stop_loss.order_type not in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT):
        raise ValueError(
            f"bracket stop_loss must be STOP_LOSS or STOP_LOSS_LIMIT, not {stop_loss.order_type}"
        )
    if take_profit.side == entry.side or stop_loss.side == entry.side:
        raise ValueError("bracket children must be on the opposite side of the entry")
    if take_profit.amount != entry.amount or stop_loss.amount != entry.amount:
        raise ValueError("bracket children must have the same amount as the entry")


@dataclass(frozen=True)
class DepthBook:
    """Order-book state delivered to ``subscribe("depth")`` callbacks.

    Built from the transport's own book (raw ``ticker.domTicks`` applied with IB semantics),
    never from ib_async's ``domBids``/``domAsks``. Rows are Decimal ``(price, size)``, best first.
    """

    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]


@dataclass
class SubscriptionHandle:
    """Handle returned by IbGatewayTransport.subscribe(). Owns cleanup on close().

    ``error`` is set (and the handle closed) when IB fails the stream itself, e.g. error 101 or
    a hard permission code on a trades or depth stream; it stays None for caller-initiated closes.
    """

    channel: str
    contract: object
    _cleanup: Callable[[], None]
    _closed: bool = field(default=False, init=False)
    error: Exception | None = field(default=None, init=False)

    @property
    def closed(self) -> bool:
        """True once the handle was closed, by the caller or after a stream failure."""
        return self._closed

    def close(self) -> None:
        """Unregister event handler and cancel IB stream. Idempotent."""
        if not self._closed:
            self._closed = True
            self._cleanup()


@dataclass
class _ActiveSubscription:
    """Registry entry for one subscriber; the contract's shared stream feeds ``dispatch``."""

    event_key: str
    channel: str
    contract: Any
    dispatch: Callable[[Any], None]


@dataclass
class _Stream:
    """A persistent per-contract IB stream (L1 reqMktData or shared reqMktDepth).

    ``hook`` is the ticker.updateEvent handler; it stays attached to the current
    ``ticker`` and moves to the new one on resubscribe. Depth streams also own
    the book (``bids``/``asks``), a user count and the idle-release timer.
    """

    contract: Any
    hook: Callable[[Any], None]
    ticker: Any = None
    waiters: list[asyncio.Future[None]] = field(default_factory=list)
    bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    refs: int = 0
    idle: asyncio.TimerHandle | None = None
    # Depth only: fail callbacks of the subscriptions sharing this stream.
    subscribers: list[Callable[[Exception], None]] = field(default_factory=list)
    # IB reqId of the current ticker (keys the transport's reqId map) and the last SOFT
    # permission warning (code, text) IB sent against it.
    req_id: int | None = None
    permission_warning: tuple[int, str] | None = None

    def wake(self) -> None:
        """Wake every coroutine waiting for new data on this stream."""
        for fut in self.waiters:
            if not fut.done():
                fut.set_result(None)


@dataclass
class _PnlFeed:
    """A live reqPnL / reqPnLSingle request shared by streams and snapshots.

    ``handler`` is hooked on pnlEvent / pnlSingleEvent for the feed's lifetime (it survives
    resubscribes); ``latest`` is the last converted value, ``waiters`` the snapshot futures
    awaiting the next one.
    """

    key: tuple[Any, ...]
    handler: Callable[[Any], None]
    refs: int = 0
    latest: Any = None
    callbacks: list[Callable[[Any], None]] = field(default_factory=list)
    waiters: list[asyncio.Future[Any]] = field(default_factory=list)


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

    def __init__(self, spec: IbConnectionSpec, *, pacer: IbPacer | None = None) -> None:
        if IB is None:
            raise ImportError(
                "ib_async is required for IbGatewayTransport. "
                "Install with: pixi add --feature interactive-brokers ib_async"
            )
        self._spec = spec
        self._ib = IB()
        # Resolved on connect: the configured account, else the single managed one.
        self._account: str | None = spec.account_id
        # Negotiated at every (re)connect; kept across disconnects.
        self._server_info: IbServerInfo | None = None
        self._handle_registry: dict[int, OrderHandle] = {}
        # Live PnL requests, one refcounted feed per (account, "") / (account, "", con_id);
        # streams and snapshots on the same key share it.
        self._pnl_feeds: dict[tuple[Any, ...], _PnlFeed] = {}
        self.pacer: IbPacer = pacer if pacer is not None else IbPacer()
        self._error_router = _ErrorRouter(
            market_data_type=spec.resolved_market_data_type,
            on_pacing_violation=self.pacer.report_pacing_violation,
        )
        self._first_status_timeout: float = 30.0
        self._event_bus = EventBus(name="ib-ws")
        self._subscribe_seq: int = 0
        self._subscriptions: dict[str, _ActiveSubscription] = {}
        # Persistent per-contract streams: one L1 ticker and one shared depth stream each.
        self._l1_streams: dict[Hashable, _Stream] = {}
        self._depth_streams: dict[Hashable, _Stream] = {}
        # IB reqId -> (stream, is_depth) for the streams' current requests; errors route by it.
        self._stream_by_req_id: dict[int, tuple[_Stream, bool]] = {}
        self._trades_streams: dict[Hashable, _Stream] = {}
        self._trades_by_req_id: dict[int, _Stream] = {}
        # IB-side market-data streams held (L1, depth, trades), capped by spec.market_data_lines.
        self._md_lines: int = 0
        # Fill de-duplication: execId base (final ".NN" removed) -> last full execId.
        self._seen_fills: OrderedDict[str, str] = OrderedDict()
        self._seen_fees: OrderedDict[str, None] = OrderedDict()
        # Reconnect supervisor state.
        self._handlers_attached: bool = False
        self._closing: bool = False
        self._connecting: bool = False
        self._supervisor: asyncio.Task[None] | None = None
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # patchable in tests
        self._error_router._connection_listeners.append(self._on_router_connection)
        self._error_router._data_lost_listeners.append(self._on_data_lost)
        self._error_router._farm_listeners.append(self._on_farm_status)
        self._error_router._depth_reset_listeners.append(self._clear_depth_books)
        self._error_router._stream_error_listeners.append(self._on_stream_error)

    @property
    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    @property
    def market_data_lines_in_use(self) -> int:
        """Market-data lines (L1, depth and trades streams) this transport currently holds."""
        return self._md_lines

    def _require_free_line(self) -> None:
        """Fail fast, before any IB request, when every market-data line is taken."""
        cap = self._spec.market_data_lines
        if self._md_lines >= cap:
            raise MarketDataLinesExhaustedError(
                _MAX_TICKERS_CODE,
                f"all {cap} market-data lines (IbConnectionSpec.market_data_lines) are in use",
            )

    def _take_line(self) -> None:
        """Count one more held line; warn when usage crosses 80% of the cap."""
        cap = self._spec.market_data_lines
        self._md_lines += 1
        if self._md_lines * 5 >= cap * 4 > (self._md_lines - 1) * 5:
            logger.warning("IB market-data lines at %d of %d (80%% reached)", self._md_lines, cap)

    def _release_line(self) -> None:
        self._md_lines -= 1

    @property
    def account(self) -> str | None:
        """Account orders, balances and positions are scoped to; None until one is known."""
        return self._account

    @property
    def server_info(self) -> IbServerInfo | None:
        """Server version from the latest successful (re)connect; None before the first."""
        return self._server_info

    async def connect(self) -> None:
        """Connect to IB Gateway, retrying timeouts a bounded number of times.

        Auth is handled by Gateway externally (interactive 2FA login at Gateway
        startup); this just opens the socket. ``spec.connect_timeout`` is passed
        to ib_async and bounds the handshake and each startup sync request.

        Retried: builtin ``TimeoutError`` and ib_async's sync-request failure
        (``ConnectionError`` whose first arg is a list, e.g.
        ``ConnectionError(["executions request timed out"])``). Anything else
        (refused connection, ``IbAccountConfigError``, ...) is raised at once.
        Up to ``spec.connect_attempts`` tries are made with exponential backoff
        from ``reconnect_initial_delay``, capped at ``reconnect_max_delay``; the
        last error is raised when all fail. On failure no handlers stay attached
        and no state is published.

        The reconnect supervisor only covers drops of an established session; a
        failed initial attempt never starts it. Known limit: ib_async surfaces a
        client-id clash (IB error 326) as a ``TimeoutError``, so it is retried up
        to ``connect_attempts`` times before being raised.
        """
        self._closing = False
        self._connecting = True
        try:
            delay = self._spec.reconnect_initial_delay
            for attempt in range(1, self._spec.connect_attempts + 1):
                try:
                    await self._connect_once()
                except Exception as exc:
                    if not _is_retryable_connect_error(exc) or (
                        attempt >= self._spec.connect_attempts
                    ):
                        raise
                    logger.warning(
                        "IB connect attempt %d/%d failed (retry in %.1fs): %r",
                        attempt,
                        self._spec.connect_attempts,
                        delay,
                        exc,
                    )
                    await self._sleep(delay)
                    delay = min(delay * 2, self._spec.reconnect_max_delay)
                else:
                    break
        finally:
            self._connecting = False
        self._publish_state("connected")

    async def _connect_once(self) -> None:
        """Run the connect sequence (used by connect() and the supervisor).

        Handlers are attached only after connectAsync returns, and only once
        across reconnects, so a failed attempt leaves nothing behind.
        """
        if self._spec.pace_api:
            self._ib.client.setConnectOptions("+PACEAPI")
        await self._open_socket()
        self._capture_server_info()
        self._resolve_account()
        try:
            self._attach_handlers()
            self._apply_market_data_type()
            await self._reconcile_executions()
            await self._sync_all_open_orders()
        except BaseException:
            self._detach_handlers()
            raise
        self._error_router.set_connected(True)

    async def _open_socket(self) -> None:
        """connectAsync, turning a version refusal into IbVersionIncompatibleError.

        ib_async reports a client/server version refusal only via ``client.apiError``
        and then times out; capture the message for the duration of this attempt.
        """
        client = self._ib.client
        last_message: list[str] = []

        def on_api_error(message: object) -> None:
            last_message.append(str(message))

        client.apiError += on_api_error
        try:
            await self._ib.connectAsync(
                host=self._spec.host,
                port=self._spec.port,
                clientId=self._spec.client_id,
                readonly=self._spec.readonly,
                account=self._spec.account_id or "",
                timeout=self._spec.connect_timeout,
                raiseSyncErrors=True,
            )
        except Exception as exc:
            if _is_order_sync_timeout(exc):
                raise IbReadOnlyApiError(
                    "IB Gateway did not answer the startup order sync; it is likely "
                    "configured with READ_ONLY_API=yes. Disable READ_ONLY_API or connect "
                    "with IbConnectionSpec(readonly=True)."
                ) from exc
            refused = [m for m in last_message if "version must be" in m.lower()]
            if not refused:
                raise
            refusal = refused[-1]
            low = getattr(client, "MinClientVersion", "?")
            high = getattr(client, "MaxClientVersion", "?")
            raise IbVersionIncompatibleError(
                f"IB refused the connection: {refusal}. "
                f"ib_async speaks client versions {low}..{high}; see #59"
            ) from exc
        finally:
            client.apiError -= on_api_error

    def _capture_server_info(self) -> None:
        """Record the negotiated server version and log it (one line per connect)."""
        client = self._ib.client
        server_version = client.serverVersion()
        if not isinstance(server_version, int) or isinstance(server_version, bool):
            return
        max_version = getattr(client, "MaxClientVersion", None)
        if not isinstance(max_version, int) or isinstance(max_version, bool):
            max_version = None
        self._server_info = IbServerInfo(server_version, max_version)
        logger.info(
            "IB connected: server version %d (ib_async max client version %s)",
            server_version,
            max_version,
        )

    def _resolve_account(self) -> None:
        """Pick the account to trade on from IB's managed list, or reject the configuration.

        Disconnects before raising so a misconfigured session never stays open.
        """
        accounts = list(self._ib.managedAccounts())
        configured = self._spec.account_id
        if configured is None and len(accounts) > 1:
            self._ib.disconnect()
            raise IbAccountConfigError(
                f"IB manages several accounts ({', '.join(accounts)}); "
                "set IbConnectionSpec.account_id to choose one"
            )
        if configured is not None and accounts and configured not in accounts:
            self._ib.disconnect()
            raise IbAccountConfigError(
                f"IbConnectionSpec.account_id {configured!r} is not managed by this login; "
                f"available accounts: {', '.join(accounts)}"
            )
        self._account = configured or (accounts[0] if accounts else None)

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

    async def _sync_all_open_orders(self) -> None:
        """Pull open orders of all API clients via reqAllOpenOrdersAsync.

        Runs after every successful (re)connect when ``fetch_all_open_orders``
        is set. Failure is logged and never fails the connect.
        """
        if self._spec.readonly or not self._spec.fetch_all_open_orders:
            return
        try:
            await self._ib.reqAllOpenOrdersAsync()
        except Exception as exc:
            logger.warning("IB all-open-orders sync failed: %s", exc)

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
        if self._closing or self._connecting:
            return  # a failed initial attempt fires this; connect() owns its retries
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
            except IbAccountConfigError as exc:
                # Retrying cannot fix a configuration error: stop and report disconnected.
                logger.error("IB reconnect aborted, account configuration invalid: %s", exc)
                self._publish_state("disconnected", message=str(exc))
                return
            except IbVersionIncompatibleError as exc:
                logger.error("IB reconnect aborted, version incompatible: %s", exc)
                self._publish_state("disconnected", message=str(exc))
                return
            except Exception as exc:
                logger.warning("IB reconnect attempt failed (next delay %.1fs): %r", delay, exc)
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
        self._release_all_streams()
        self._detach_handlers()
        self._error_router.set_connected(False)
        self._ib.disconnect()

    async def get_balance(self, currency: str) -> Decimal:
        """Return the balance of *currency* for the resolved account; 0 when IB reports none.

        Hybrid rule: IB reports AvailableFunds only in the account's base currency,
        so use it when present and fall back to TotalCashValue (reported per currency).
        """
        rows = self._ib.accountValues(self.account or "")
        wanted = currency.upper()
        by_tag: dict[str, str] = {}
        for row in rows:
            if self.account is not None and row.account != self.account:
                continue
            if row.currency.upper() == wanted and row.tag in ("AvailableFunds", "TotalCashValue"):
                by_tag[row.tag] = row.value
        value = by_tag.get("AvailableFunds", by_tag.get("TotalCashValue"))
        return Decimal(value) if value is not None else Decimal("0")

    async def get_positions(self) -> list[IbPosition]:
        """Return the open positions of the resolved account (all managed accounts if unset)."""
        from market_connector.exchanges.interactive_brokers.positions import (  # noqa: PLC0415
            IbPosition,
        )

        return [
            IbPosition(
                account=pos.account,
                symbol=pos.contract.symbol,
                sec_type=pos.contract.secType,
                con_id=pos.contract.conId,
                exchange=pos.contract.exchange or pos.contract.primaryExchange,
                currency=pos.contract.currency,
                quantity=Decimal(str(pos.position)),
                avg_cost=Decimal(str(pos.avgCost)),
            )
            for pos in self._ib.positions(self.account or "")
        ]

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "IbGatewayTransport does not implement request(); "
            "use typed methods on IbGatewayTransport directly"
        )

    async def match_symbols(self, pattern: str) -> list[Any]:
        """Return IB ContractDescriptions matching pattern (reqMatchingSymbols); [] if none."""
        await self.pacer.matching_symbols()
        return await self._ib.reqMatchingSymbolsAsync(pattern) or []

    async def _resolve_via_ib(self, ref: InstrumentRef) -> list:
        """Call ib_async.reqContractDetailsAsync with a Contract template built from ref.

        Returns the raw list of ContractDetails objects. Ambiguity checking and
        ResolvedContract construction live in IbContractResolver; this method is
        a thin I/O wrapper.
        """
        from ib_async import Contract  # noqa: PLC0415 — lazy, ib_async is optional

        from market_connector.contracts.instrument import InstrumentType  # noqa: PLC0415
        from market_connector.exchanges.interactive_brokers.contract_resolver import (  # noqa: PLC0415
            _INSTRUMENT_TYPE_TO_DEFAULT_EXCHANGE,
            _INSTRUMENT_TYPE_TO_SECTYPE,
        )

        hint = getattr(ref, "exchange_hint", None)
        if ref.con_id is not None:
            # conId is unique on its own; extra fields would only risk a mismatch.
            template = Contract(conId=ref.con_id, exchange=hint or "SMART")
            return await self._ib.reqContractDetailsAsync(template)

        if hint is None and ref.instrument_type is InstrumentType.FUTURE:
            raise ValueError(
                f"{ref.symbol}: an exchange is required for futures (no exchange_hint or con_id)"
            )
        exchange = hint or _INSTRUMENT_TYPE_TO_DEFAULT_EXCHANGE.get(ref.instrument_type, "SMART")

        fields: dict[str, Any] = {
            "symbol": ref.symbol,
            "secType": _INSTRUMENT_TYPE_TO_SECTYPE.get(ref.instrument_type, "STK"),
            "currency": ref.quote_currency or "USD",
            "exchange": exchange,
        }
        if ref.primary_exchange:
            fields["primaryExchange"] = ref.primary_exchange
        if ref.contract_month:
            fields["lastTradeDateOrContractMonth"] = ref.contract_month
        elif ref.expiry is not None:
            fields["lastTradeDateOrContractMonth"] = ref.expiry.strftime("%Y%m%d")
        if ref.local_symbol:
            fields["localSymbol"] = ref.local_symbol
        if ref.strike is not None:
            fields["strike"] = float(ref.strike)
        if ref.option_right is not None:
            fields["right"] = ref.option_right.value[0]  # CALL -> "C", PUT -> "P"
        template = Contract(**fields)
        return await self._ib.reqContractDetailsAsync(template)

    async def place_order(
        self,
        native_contract: Any,
        hb_order: HBOrder,
        *,
        options: IbOrderOptions | None = None,
    ) -> OrderHandle:
        """Submit an order to IB and await the first non-PendingSubmit status update.

        The caller (gateway) is responsible for pre-resolving the contract; this
        method receives the raw ib_async Contract object directly. ``options``
        carries opt-in IB-specific settings (e.g. the Adaptive algo).

        Returns an OrderHandle snapshot reflecting the first meaningful status.
        Raises ConnectionLostError if not connected, ValueError if ``options`` do
        not fit the contract or order type (before any IB call), OrderRejectedError
        if IB rejects the order before statusEvent fires, or asyncio.TimeoutError
        if no status arrives within _first_status_timeout seconds.
        """
        self._require_writable("place_order")
        if not self._error_router.is_connected:
            from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
                ConnectionLostError,
            )

            raise ConnectionLostError(1100, "transport not connected")

        ib_order = _hb_to_ib_order(hb_order)
        _apply_order_options(ib_order, native_contract, hb_order, options)
        trade = self._submit_leg(native_contract, ib_order)
        return await self._await_leg(trade)

    def _submit_leg(self, native_contract: Any, ib_order: Any) -> Any:
        """Stamp the account, hand *ib_order* to IB and register its initial handle.

        Synchronous on purpose: bracket legs must reach IB back to back, with no await between.
        """
        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
        )

        if self.account is not None:
            ib_order.account = self.account
        trade = self._ib.placeOrder(native_contract, ib_order)
        self._handle_registry[trade.order.orderId] = OrderHandle.from_trade(
            trade, self._spec.client_id
        )
        return trade

    async def _await_leg(self, trade: Any) -> OrderHandle:
        """Await the first status of a submitted *trade* and record the resulting handle."""
        try:
            new_handle = await self._wait_first_status_update(trade)
        except OrderRejectedError as exc:
            # The router has no trade; attach IB's advanced reject text here.
            raise OrderRejectedError(
                exc.error_code, exc.message, advanced_reject=trade.advancedError or None
            ) from exc
        self._handle_registry[trade.order.orderId] = new_handle
        return new_handle

    def _require_writable(self, op: str) -> None:
        """Raise IbReadOnlyModeError when the session was opened with ``readonly=True``."""
        if self._spec.readonly:
            raise IbReadOnlyModeError(
                f"{op} is not allowed: the transport is connected with "
                "IbConnectionSpec(readonly=True)"
            )

    def _require_connected(self) -> None:
        if not self._error_router.is_connected:
            raise ConnectionLostError(1100, "transport not connected")

    async def place_bracket(
        self, native_contract: Any, entry: HBOrder, take_profit: HBOrder, stop_loss: HBOrder
    ) -> IbBracket:
        """Place an entry with attached take-profit and stop-loss children as one bracket.

        The entry must be MARKET/LIMIT, the take-profit LIMIT and the stop-loss
        STOP_LOSS/STOP_LOSS_LIMIT; both children take the opposite side and the
        entry's amount. All validation runs before any IB call (ValueError). The
        three legs are sent back to back with the transmit flag only on the last,
        so IB activates the bracket atomically.

        If any leg fails (rejected, timed out or otherwise), every other placed leg
        not known to be rejected is cancelled. A rejection then raises
        LinkedOrderRejectedError (see ``place_oca``); any other failure re-raises the
        original exception after the rollback. Raises ConnectionLostError if not
        connected.
        """
        from market_connector.exchanges.interactive_brokers.linked_orders import (  # noqa: PLC0415
            IbBracket,
        )

        self._require_writable("place_bracket")
        self._require_connected()
        _validate_bracket(entry, take_profit, stop_loss)
        ib_orders = [_hb_to_ib_order(o) for o in (entry, take_profit, stop_loss)]
        parent_id = self._ib.client.getReqId()
        ids = [parent_id, self._ib.client.getReqId(), self._ib.client.getReqId()]
        for ib_order, order_id, transmit in zip(ib_orders, ids, (False, False, True), strict=True):
            ib_order.orderId = order_id
            ib_order.parentId = 0 if order_id == parent_id else parent_id
            ib_order.transmit = transmit
        trades = [self._submit_leg(native_contract, o) for o in ib_orders]
        parent, tp_handle, sl_handle = await self._settle_legs(trades)
        return IbBracket(parent=parent, take_profit=tp_handle, stop_loss=sl_handle)

    async def place_oca(
        self,
        native_contract: Any,
        orders: Sequence[HBOrder],
        oca_type: OcaType = OcaType.CANCEL_WITH_BLOCK,
    ) -> IbOcaGroup:
        """Place *orders* as one OCA (one-cancels-all) group under a fresh group name.

        Needs at least two orders (ValueError before any IB call). Handles come
        back in input order once every member has a first status. If any member
        fails (rejected, timed out or otherwise), every other placed member not known
        to be rejected is cancelled. A rejection raises LinkedOrderRejectedError,
        carrying all handles and any cancel failures; any other failure re-raises the
        original exception after the rollback. Raises ConnectionLostError if not
        connected. Cancelling any one member makes IB cancel the whole group.
        """
        from market_connector.exchanges.interactive_brokers.linked_orders import (  # noqa: PLC0415
            IbOcaGroup,
        )

        self._require_writable("place_oca")
        self._require_connected()
        if len(orders) < 2:
            raise ValueError(f"an OCA group needs at least 2 orders, got {len(orders)}")
        ib_orders = [_hb_to_ib_order(o) for o in orders]
        group = f"hb-oca-{uuid.uuid4().hex}"
        for ib_order in ib_orders:
            ib_order.ocaGroup = group
            ib_order.ocaType = int(oca_type)
            ib_order.transmit = True
        trades = [self._submit_leg(native_contract, o) for o in ib_orders]
        handles = await self._settle_legs(trades)
        return IbOcaGroup(group=group, oca_type=oca_type, orders=handles)

    async def _settle_legs(self, trades: list[Any]) -> tuple[OrderHandle, ...]:
        """Await the first status of every leg; roll the group back if any leg fails.

        Rollback rule: only legs known dead (failed with OrderRejectedError) are skipped.
        Every other placed leg, including one that timed out or failed otherwise (it may
        be live at IB), is cancelled; ``cancel_order`` leaves already-terminal trades
        alone. Cancel failures are collected. A rejection raises
        LinkedOrderRejectedError from the original; any other failure (e.g. a status
        timeout) is re-raised unchanged after the rollback.
        """
        results = await asyncio.gather(
            *(self._await_leg(t) for t in trades), return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        if not failures:
            return tuple(r for r in results if not isinstance(r, BaseException))
        rejected = next((r for r in failures if isinstance(r, OrderRejectedError)), None)

        handles = tuple(
            self._handle_registry[t.order.orderId] if isinstance(r, BaseException) else r
            for t, r in zip(trades, results, strict=True)
        )
        cancel_errors: dict[int, Exception] = {}
        for handle, result in zip(handles, results, strict=True):
            if isinstance(result, OrderRejectedError):
                continue
            try:
                await self.cancel_order(handle)
            except Exception as exc:  # noqa: BLE001
                if rejected is None:
                    logger.exception("IB rollback cancel of order %s failed", handle.order_id)
                cancel_errors[handle.order_id] = exc
        if rejected is None:
            raise failures[0]
        raise LinkedOrderRejectedError(
            rejected.error_code,
            rejected.message,
            rejected.advanced_reject,
            handles=handles,
            cancel_errors=cancel_errors,
        ) from rejected

    async def what_if(
        self,
        native_contract: Any,
        hb_order: HBOrder,
        *,
        options: IbOrderOptions | None = None,
    ) -> IbWhatIfResult:
        """Ask IB for the margin and commission impact of an order without placing it.

        ``options`` (e.g. the Adaptive algo) are previewed exactly as place_order
        would send them. Nothing is submitted or tracked. Raises IbReadOnlyModeError
        in read-only mode (IB treats whatIf as an order-API request, so a READ_ONLY_API
        Gateway never answers), ConnectionLostError if not connected,
        ValueError/NotImplementedError for orders or ``options``
        that cannot be translated (before any IB call), WhatIfTimeoutError if IB does not
        answer within ``what_if_timeout``; IB errors propagate unchanged.
        """
        self._require_writable("what_if")
        if not self._error_router.is_connected:
            raise ConnectionLostError(1100, "transport not connected")

        from market_connector.exchanges.interactive_brokers.what_if import (  # noqa: PLC0415
            IbWhatIfResult,
        )

        ib_order = _hb_to_ib_order(hb_order)
        _apply_order_options(ib_order, native_contract, hb_order, options)
        if self.account is not None:
            ib_order.account = self.account
        timeout = self._spec.what_if_timeout
        try:
            state = await asyncio.wait_for(
                self._ib.whatIfOrderAsync(native_contract, ib_order), timeout=timeout
            )
        except TimeoutError as exc:
            raise WhatIfTimeoutError(0, f"no what-if answer within {timeout}s") from exc
        return IbWhatIfResult.from_order_state(state)

    def _pnl_account(self) -> str:
        """Return the resolved account for a PnL request; guards connection and account."""
        if not self._error_router.is_connected:
            raise ConnectionLostError(1100, "transport not connected")
        if self.account is None:
            raise IbAccountConfigError(
                "no IB account is resolved; set IbConnectionSpec.account_id to request PnL"
            )
        return self.account

    def _acquire_pnl(self, key: tuple[Any, ...]) -> _PnlFeed:
        """Return the live feed for *key* (opening its IB request) with one more user."""
        feed = self._pnl_feeds.get(key)
        if feed is None:
            feed = _PnlFeed(key=key, handler=lambda _pnl: None)
            feed.handler = self._make_pnl_handler(feed)
            event = self._pnl_event(key)
            event += feed.handler
            try:
                self._pnl_request(key)
            except Exception:
                event -= feed.handler
                raise
            self._pnl_feeds[key] = feed
        feed.refs += 1
        return feed

    def _release_pnl(self, feed: _PnlFeed) -> None:
        """Drop one user; the last one unhooks the feed and cancels it at IB."""
        feed.refs -= 1
        if feed.refs <= 0 and self._pnl_feeds.get(feed.key) is feed:
            self._drop_pnl_feed(feed)

    def _drop_pnl_feed(self, feed: _PnlFeed, *, exc: Exception | None = None) -> None:
        """Forget *feed*, detach its handler, fail snapshot waiters with *exc*, cancel at IB."""
        self._pnl_feeds.pop(feed.key, None)
        event = self._pnl_event(feed.key)
        event -= feed.handler
        if exc is not None:
            for fut in feed.waiters:
                if not fut.done():
                    fut.set_exception(exc)
        feed.waiters.clear()
        try:
            self._pnl_cancel(feed.key)
        except Exception as cancel_exc:
            logger.warning("IB cancel of PnL feed %r failed: %s", feed.key, cancel_exc)

    def _pnl_event(self, key: tuple[Any, ...]) -> Any:
        return self._ib.pnlSingleEvent if len(key) == 3 else self._ib.pnlEvent

    def _pnl_request(self, key: tuple[Any, ...]) -> None:
        if len(key) == 3:
            self._ib.reqPnLSingle(*key)
        else:
            self._ib.reqPnL(*key)

    def _pnl_cancel(self, key: tuple[Any, ...]) -> None:
        if len(key) == 3:
            self._ib.cancelPnLSingle(*key)
        else:
            self._ib.cancelPnL(*key)

    def _make_pnl_handler(self, feed: _PnlFeed) -> Callable[[Any], None]:
        """Build the event handler of *feed*: filter by key, convert, store, wake, fan out."""
        from market_connector.exchanges.interactive_brokers.pnl import (  # noqa: PLC0415
            IbPnL,
            IbPositionPnL,
        )

        account = feed.key[0]
        single = len(feed.key) == 3

        def handler(pnl: Any) -> None:
            if pnl.account != account or (single and pnl.conId != feed.key[2]):
                return
            value = IbPositionPnL.from_pnl_single(pnl) if single else IbPnL.from_pnl(pnl)
            feed.latest = value
            for fut in feed.waiters:
                if not fut.done():
                    fut.set_result(value)
            feed.waiters.clear()
            for callback in list(feed.callbacks):
                try:
                    callback(value)
                except Exception:
                    logger.exception("IB PnL callback for %r raised", feed.key)

        return handler

    async def _snapshot_pnl(self, key: tuple[Any, ...]) -> Any:
        """Return the feed's latest value, awaiting the next one if none has arrived yet."""
        feed = self._acquire_pnl(key)
        try:
            if feed.latest is not None:
                return feed.latest
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            feed.waiters.append(future)
            try:
                return await self._await_pnl(future)
            finally:
                if future in feed.waiters:
                    feed.waiters.remove(future)
        finally:
            self._release_pnl(feed)

    def _subscribe_pnl_feed(
        self, key: tuple[Any, ...], callback: Callable[[Any], None], channel: str, contract: object
    ) -> SubscriptionHandle:
        feed = self._acquire_pnl(key)
        feed.callbacks.append(callback)

        def cleanup() -> None:
            with contextlib.suppress(ValueError):
                feed.callbacks.remove(callback)
            self._release_pnl(feed)

        return SubscriptionHandle(channel=channel, contract=contract, _cleanup=cleanup)

    async def get_pnl(self) -> IbPnL:
        """Return an account PnL snapshot (reqPnL, first answer, then cancelPnL).

        Raises ConnectionLostError if not connected, IbAccountConfigError if no account
        is resolved, PnlTimeoutError if IB does not answer within ``pnl_timeout``.
        Concurrent calls share one IB subscription; with an active ``subscribe_pnl``
        stream the stream's latest value is returned and nothing is requested or
        cancelled. The first answer may be stale; take a second reading (or stream)
        for a settled daily figure.
        """
        result: IbPnL = await self._snapshot_pnl((self._pnl_account(), ""))
        return result

    async def get_position_pnl(self, con_id: int) -> IbPositionPnL:
        """Return a PnL snapshot for one position (reqPnLSingle, then cancel).

        Same errors and sharing semantics as ``get_pnl``, keyed per ``con_id``.
        """
        result: IbPositionPnL = await self._snapshot_pnl((self._pnl_account(), "", con_id))
        return result

    def subscribe_pnl(self, callback: Callable[[IbPnL], None]) -> SubscriptionHandle:
        """Stream account PnL updates to *callback*; one shared IB request per account.

        Raises ConnectionLostError if not connected, IbAccountConfigError if no account
        is resolved. The stream survives reconnects and is released by ``disconnect()``;
        the IB request is cancelled when the last subscriber (and snapshot) leaves.
        """
        return self._subscribe_pnl_feed((self._pnl_account(), ""), callback, "pnl", None)

    def subscribe_position_pnl(
        self, con_id: int, callback: Callable[[IbPositionPnL], None]
    ) -> SubscriptionHandle:
        """Stream one position's PnL updates to *callback*; shared per ``con_id``.

        Same errors and lifecycle as ``subscribe_pnl``.
        """
        return self._subscribe_pnl_feed(
            (self._pnl_account(), "", con_id), callback, "pnl_single", con_id
        )

    async def _await_pnl(self, future: asyncio.Future[Any]) -> Any:
        timeout = self._spec.pnl_timeout
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise PnlTimeoutError(0, f"no PnL answer within {timeout}s") from exc

    async def _wait_first_status_update(self, trade: Any) -> OrderHandle:
        """Await the first meaningful status update for a submitted trade.

        Either statusEvent resolves the future (success path) or the error router
        sets an exception on it (error-before-status race). The try/finally block
        guarantees cleanup of both the statusEvent hook and the pending waiter entry.
        """
        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            OrderHandle,
            is_synthetic_cancel,
            is_unacknowledged,
        )

        fut: asyncio.Future[OrderHandle] = asyncio.get_running_loop().create_future()
        self._error_router._pending_order_waiters[trade.order.orderId] = fut

        def _on_status(*_args: Any) -> None:
            # A synthetic cancel from an informational code is not IB's verdict; keep waiting.
            if is_synthetic_cancel(trade):
                return
            # PendingSubmit/ApiPending are not an acknowledgement; keep waiting.
            if is_unacknowledged(trade):
                return
            try:
                new_handle = OrderHandle.from_trade(trade, self._spec.client_id)
            except ValueError:
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
            IbReadOnlyModeError: If the transport is connected with ``readonly=True``.
        """
        self._require_writable("cancel_order")
        from market_connector.exchanges.interactive_brokers.order_handle import (  # noqa: PLC0415
            IBOrderState,
            OrderHandle,
        )

        if not handle.owned:
            raise ValueError(
                f"order {handle.order_id} belongs to client_id {handle.client_id}, not this "
                f"client ({self._spec.client_id}); cannot cancel orders of another API client"
            )

        terminals = {IBOrderState.FILLED, IBOrderState.CANCELLED, IBOrderState.REJECTED}
        if handle.status in terminals:
            return handle

        self._ib.cancelOrder(handle._trade.order)

        fut: asyncio.Future[OrderHandle] = asyncio.get_running_loop().create_future()

        def _on_status(*_args: Any) -> None:
            try:
                new_handle = OrderHandle.from_trade(handle._trade, self._spec.client_id)
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
            handle = OrderHandle.from_trade(trade, self._spec.client_id)
            self._handle_registry[order_id] = handle
            result.append(handle)
        return result

    async def cancel_all(self) -> list[OrderHandle]:
        """Cancel every open order owned by this client, sequentially.

        Orders of other API clients and manual TWS orders are skipped;
        reqGlobalCancel is never used. Every eligible order is attempted.
        Raises ConnectionLostError if the transport is not connected (unwrapped).
        Raises CancelAllError (an ExceptionGroup whose ``cancelled`` lists the
        successful cancels) if any individual cancel fails.
        Raises IbReadOnlyModeError if connected with ``readonly=True``.
        """
        self._require_writable("cancel_all")
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            CancelAllError,
        )

        targets = [h for h in self.open_orders() if h.owned and not h.manual]
        cancelled: list[OrderHandle] = []
        errors: list[Exception] = []
        failed_ids: list[int] = []
        for handle in targets:
            try:
                cancelled.append(await self.cancel_order(handle))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                failed_ids.append(handle.order_id)
        if errors:
            ids = ", ".join(str(i) for i in failed_ids)
            raise CancelAllError(
                f"cancel_all: {len(errors)} of {len(targets)} cancels failed (order ids: {ids})",
                errors,
                cancelled,
            )
        return cancelled

    def subscribe(
        self,
        channel: str,
        contract: Contract,
        callback: Callable[[Any], None],
        *,
        on_error: Callable[[Exception], None] | None = None,
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
            callback: sync Callable[[ticker_or_tick], None]. Never receives an exception.
            on_error: optional keyword-only Callable[[Exception], None]. It is called once
                with the typed error when IB reports 101 (MarketDataLinesExhaustedError) or a
                hard permission code (MarketDataPermissionError) on the stream's reqId; the
                handle is closed and ``handle.error`` set first. Every subscriber sharing the
                contract's stream is failed the same way and the stream's line is released
                and the IB stream cancelled once. An exception raised by it is logged.

        Returns:
            SubscriptionHandle whose .close() unregisters the handler and
            cancels the IB stream.
        """
        if channel not in ("depth", "trades"):
            raise ValueError(f"Unknown subscribe channel {channel!r}; expected 'depth' or 'trades'")

        self._subscribe_seq += 1
        event_key = f"{channel}:{self._subscribe_seq}"
        bus_subscription = self._event_bus.subscribe(event_key, callback)

        def _dispatch_depth(book: DepthBook) -> None:
            self._event_bus.publish(event_key, book)

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
        try:
            # Subscribers share the contract's per-channel stream, which feeds each of them.
            stream = (
                self._acquire_depth(contract)
                if channel == "depth"
                else self._acquire_trades(contract)
            )
        except Exception:
            self._event_bus.unsubscribe(bus_subscription)
            raise
        self._subscriptions[event_key] = active

        def _cleanup() -> None:
            self._subscriptions.pop(event_key, None)
            self._event_bus.unsubscribe(bus_subscription)
            if _fail in stream.subscribers:
                stream.subscribers.remove(_fail)
            if channel == "depth":
                self._release_depth(stream)
            else:
                self._release_trades(stream)

        handle = SubscriptionHandle(channel=channel, contract=contract, _cleanup=_cleanup)

        def _fail(exc: Exception) -> None:
            if handle.closed:
                return
            handle.error = exc
            handle.close()
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    logger.exception("on_error callback of %s raised", event_key)

        stream.subscribers.append(_fail)
        return handle

    def _acquire_trades(self, contract: Any) -> _Stream:
        """Return the contract's shared trades stream (opening it) with one more user."""
        key = _stream_key(contract)
        stream = self._trades_streams.get(key)
        if stream is None:

            def hook(ticker: Any, _key: Hashable = key) -> None:
                self._on_trades_update(_key, ticker)

            self._require_free_line()
            stream = _Stream(contract=contract, hook=hook)
            self._request_trades(stream)
            self._trades_streams[key] = stream
            self._take_line()
        stream.refs += 1
        return stream

    def _release_trades(self, stream: _Stream) -> None:
        """Drop one user; the last one cancels the stream and frees its line."""
        stream.refs -= 1
        if stream.refs <= 0:
            self._drop_trades(stream)

    def _on_trades_update(self, key: Hashable, ticker: Any) -> None:
        """Hand the shared ticker to every trades subscriber of the contract."""
        for sub in list(self._subscriptions.values()):
            if sub.channel == "trades" and _stream_key(sub.contract) == key:
                sub.dispatch(ticker)

    def _request_trades(self, stream: _Stream) -> None:
        """Send the IB tick-by-tick request for a trades *stream*, hook its ticker and reqId."""
        ticker, req_id = _issue_request(
            self._ib,
            "tickByTick",
            lambda: self._ib.reqTickByTickData(_native(stream.contract), "AllLast", 0, False),
        )
        ticker.updateEvent += stream.hook
        stream.ticker = ticker
        if req_id is not None:
            stream.req_id = req_id
            self._trades_by_req_id[req_id] = stream

    def _unhook_trades(self, stream: _Stream) -> None:
        """Detach the stream's hook and forget its reqId, if hooked."""
        if stream.ticker is not None:
            stream.ticker.updateEvent -= stream.hook
            stream.ticker = None
        if stream.req_id is not None:
            self._trades_by_req_id.pop(stream.req_id, None)
            stream.req_id = None

    def _drop_trades(self, stream: _Stream) -> None:
        """Forget, unhook, cancel and free the line of a trades *stream*; a no-op once dropped."""
        key = _stream_key(stream.contract)
        if self._trades_streams.get(key) is not stream:
            return
        del self._trades_streams[key]
        self._unhook_trades(stream)
        self._release_line()
        try:
            self._ib.cancelTickByTickData(_native(stream.contract), "AllLast")
        except Exception as exc:
            logger.warning("IB cancel of trades stream %r failed: %s", stream.contract, exc)

    def _resubscribe_trades(self, stream: _Stream, cancel_old: bool) -> None:
        """Move a trades stream onto a freshly requested ticker."""
        try:
            self._unhook_trades(stream)
            if cancel_old:
                self._ib.cancelTickByTickData(_native(stream.contract), "AllLast")
            self._request_trades(stream)
        except Exception as exc:
            logger.warning("IB resubscribe of trades stream %r failed: %s", stream.contract, exc)

    def resubscribe_all(self, *, cancel_old: bool = True) -> None:
        """Re-issue every open IB request on a fresh ticker.

        Covers shared trades streams, L1 tickers and shared depth streams (idle
        but still open ones included). Bus subscriptions and caller callbacks
        are untouched; only the ticker hooks move, and depth books are cleared.
        Pass ``cancel_old=False`` after a full reconnect, where the old streams
        no longer exist server-side.
        """
        for trades in list(self._trades_streams.values()):
            self._resubscribe_trades(trades, cancel_old)
        for stream in list(self._l1_streams.values()):
            self._resubscribe_stream(stream, cancel_old, depth=False)
        for stream in list(self._depth_streams.values()):
            self._resubscribe_stream(stream, cancel_old, depth=True)
        for feed in list(self._pnl_feeds.values()):
            self._resubscribe_pnl(feed, cancel_old)

    def _resubscribe_pnl(self, feed: _PnlFeed, cancel_old: bool) -> None:
        """Re-issue a PnL request; the handler, callbacks and refs stay in place."""
        feed.latest = None  # pre-drop value is stale; snapshots wait for the fresh one
        try:
            if cancel_old:
                self._pnl_cancel(feed.key)
            self._pnl_request(feed.key)
        except Exception as exc:
            logger.warning("IB resubscribe of PnL feed %r failed: %s", feed.key, exc)

    def _resubscribe_stream(self, stream: _Stream, cancel_old: bool, *, depth: bool) -> None:
        """Move a persistent stream onto a freshly requested ticker."""
        try:
            self._unhook_persistent(stream)
            stream.bids.clear()
            stream.asks.clear()
            stream.permission_warning = None
            if cancel_old:
                self._cancel_persistent(stream, depth=depth)
            self._request_persistent(stream, depth=depth)
        except Exception as exc:
            logger.warning("IB resubscribe of %s stream %r failed: %s", depth, stream.contract, exc)

    def _request_persistent(self, stream: _Stream, *, depth: bool) -> None:
        """Send the reqMktDepth / reqMktData request and hook its ticker."""
        contract = _native(stream.contract)
        if depth:
            ticker, req_id = _issue_request(
                self._ib,
                "mktDepth",
                lambda: self._ib.reqMktDepth(contract, numRows=_DEPTH_ROWS, isSmartDepth=True),
            )
        else:
            ticker, req_id = _issue_request(
                self._ib, "mktData", lambda: self._ib.reqMktData(contract)
            )
        ticker.updateEvent += stream.hook
        stream.ticker = ticker
        if req_id is not None:
            stream.req_id = req_id
            self._stream_by_req_id[req_id] = (stream, depth)

    def _unhook_persistent(self, stream: _Stream) -> None:
        """Detach the stream's hook and forget its reqId: the current request is going away."""
        if stream.ticker is not None:
            stream.ticker.updateEvent -= stream.hook
            stream.ticker = None
        if stream.req_id is not None:
            self._stream_by_req_id.pop(stream.req_id, None)
            stream.req_id = None

    def _cancel_persistent(self, stream: _Stream, *, depth: bool) -> None:
        if depth:
            self._ib.cancelMktDepth(_native(stream.contract), isSmartDepth=True)
        else:
            self._ib.cancelMktData(_native(stream.contract))

    # -- persistent streams -------------------------------------------------

    def _release_all_streams(self) -> None:
        """Cancel every L1, depth and trades stream exactly once and stop idle timers."""
        for streams, depth in ((self._l1_streams, False), (self._depth_streams, True)):
            for stream in list(streams.values()):
                self._drop_stream(streams, stream, depth=depth)
        for trades in list(self._trades_streams.values()):
            self._drop_trades(trades)
        for feed in list(self._pnl_feeds.values()):
            self._drop_pnl_feed(feed, exc=ConnectionLostError(1100, "transport disconnected"))

    def _drop_stream(
        self, streams: dict[Hashable, _Stream], stream: _Stream, *, depth: bool
    ) -> None:
        """Forget *stream*, stop its timer, detach its hook and cancel it at IB."""
        key = _stream_key(stream.contract)
        if streams.get(key) is stream:
            del streams[key]
            self._release_line()
        if stream.idle is not None:
            stream.idle.cancel()
            stream.idle = None
        self._unhook_persistent(stream)
        stream.wake()
        try:
            self._cancel_persistent(stream, depth=depth)
        except Exception as exc:
            logger.warning("IB cancel of stream %r failed: %s", stream.contract, exc)

    def _fail_stream(
        self,
        stream: _Stream,
        *,
        depth: bool,
        exc: MarketDataPermissionError | MarketDataLinesExhaustedError | None = None,
    ) -> None:
        """Release *stream* after a permission or line problem: fail waiters, cancel, forget.

        With *exc*, the depth subscribers sharing the stream are failed after it is dropped,
        so the line is released once and their later close() is a no-op.
        """
        if exc is not None:
            for fut in stream.waiters:
                if not fut.done():
                    fut.set_exception(exc)
        streams = self._depth_streams if depth else self._l1_streams
        if streams.get(_stream_key(stream.contract)) is stream:
            self._drop_stream(streams, stream, depth=depth)
        if exc is not None:
            subscribers = list(stream.subscribers)
            stream.subscribers.clear()
            for notify in subscribers:
                notify(exc)

    def _fail_trades(self, stream: _Stream, code: int, msg: str) -> None:
        """Fail every subscriber of a trades *stream* on 101 / a hard permission code.

        The stream is dropped first (line freed, cancelled once), so the subscribers'
        later close() is a no-op. Other codes leave it alone.
        """
        exc: MarketDataPermissionError | MarketDataLinesExhaustedError
        if code in _HARD_PERMISSION_CODES:
            exc = MarketDataPermissionError(code, msg)
        elif code == _MAX_TICKERS_CODE:
            exc = MarketDataLinesExhaustedError(code, msg)
        else:
            return
        self._drop_trades(stream)
        subscribers = list(stream.subscribers)
        stream.subscribers.clear()
        for notify in subscribers:
            notify(exc)

    def _on_stream_error(self, req_id: int, code: int, msg: str) -> None:
        """Route a permission or line-cap code IB reported against a persistent stream's reqId.

        Matches by reqId and code only; unknown reqIds (including -1) and other codes are ignored.
        """
        trades = self._trades_by_req_id.get(req_id)
        if trades is not None:
            self._fail_trades(trades, code, msg)
            return
        entry = self._stream_by_req_id.get(req_id)
        if entry is None:
            return
        stream, depth = entry
        if code in _HARD_PERMISSION_CODES:
            self._fail_stream(stream, depth=depth, exc=MarketDataPermissionError(code, msg))
        elif code == _MAX_TICKERS_CODE:
            self._fail_stream(stream, depth=depth, exc=MarketDataLinesExhaustedError(code, msg))
        elif code in _SOFT_PERMISSION_CODES:
            stream.permission_warning = (code, msg)

    def _acquire_depth(self, contract: Any) -> _Stream:
        """Return the contract's shared depth stream (opening it) with one more user."""
        key = _stream_key(contract)
        stream = self._depth_streams.get(key)
        if stream is None:

            def hook(ticker: Any, _key: Hashable = key) -> None:
                self._on_depth_update(self._depth_streams.get(_key), ticker)

            self._require_free_line()
            stream = _Stream(contract=contract, hook=hook)
            self._request_persistent(stream, depth=True)
            self._depth_streams[key] = stream
            self._take_line()
        if stream.idle is not None:
            stream.idle.cancel()
            stream.idle = None
        stream.refs += 1
        return stream

    def _release_depth(self, stream: _Stream) -> None:
        """Drop one user; the last one arms the idle-release timer."""
        stream.refs -= 1
        if stream.refs > 0 or self._depth_streams.get(_stream_key(stream.contract)) is not stream:
            return
        if stream.idle is not None:
            stream.idle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to run a timer on: nothing can use the stream later, release now.
            self._drop_stream(self._depth_streams, stream, depth=True)
            return
        stream.idle = loop.call_later(self._spec.depth_idle_release, self._idle_expired, stream)

    def _idle_expired(self, stream: _Stream) -> None:
        stream.idle = None
        if stream.refs <= 0 and self._depth_streams.get(_stream_key(stream.contract)) is stream:
            self._drop_stream(self._depth_streams, stream, depth=True)

    def _on_depth_update(self, stream: _Stream | None, ticker: Any) -> None:
        """Apply the update's raw ``domTicks`` to the own book, then notify."""
        if stream is None:
            return
        for tick in list(getattr(ticker, "domTicks", None) or []):
            self._apply_depth_tick(stream, tick)
        stream.wake()
        book = DepthBook(bids=tuple(stream.bids), asks=tuple(stream.asks))
        for sub in list(self._subscriptions.values()):
            if sub.channel == "depth" and sub.contract == stream.contract:
                sub.dispatch(book)

    @staticmethod
    def _apply_depth_tick(stream: _Stream, tick: Any) -> None:
        """Apply one IB depth row update (insert/update/delete) with IB's shifting rules."""
        book = stream.bids if tick.side == 1 else stream.asks
        pos = tick.position
        if tick.operation == 0:
            if 0 <= pos <= len(book):
                book.insert(pos, (Decimal(str(tick.price)), Decimal(str(tick.size))))
                del book[_DEPTH_ROWS:]
        elif 0 <= pos < len(book):
            if tick.operation == 1:
                book[pos] = (Decimal(str(tick.price)), Decimal(str(tick.size)))
            elif tick.operation == 2:
                del book[pos]

    def _clear_depth_books(self) -> None:
        """Empty every depth book (IB error 317: depth was reset)."""
        for stream in self._depth_streams.values():
            stream.bids.clear()
            stream.asks.clear()

    async def _wait_for(
        self, stream: _Stream, ready: Callable[[], Any], what: str, *, depth: bool
    ) -> Any:
        """Return ``ready()`` once it is not None, waking on *stream* updates.

        Raises MarketDataTimeoutError after ``market_data_timeout`` seconds, or
        MarketDataPermissionError (releasing the stream) if IB flagged a SOFT
        permission problem on it and no data arrived.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._spec.market_data_timeout
        while True:
            result = ready()
            if result is not None:
                return result
            remaining = deadline - loop.time()
            if remaining <= 0:
                if stream.permission_warning is not None:
                    code, msg = stream.permission_warning
                    # Shared depth streams may feed other holders: release only if sole holder.
                    if stream.refs <= 1:
                        self._fail_stream(stream, depth=depth)
                    raise MarketDataPermissionError(code, msg)
                raise MarketDataTimeoutError(
                    0, f"no {what} for {stream.contract!r} within {self._spec.market_data_timeout}s"
                )
            fut: asyncio.Future[None] = loop.create_future()
            stream.waiters.append(fut)
            try:
                await asyncio.wait_for(fut, remaining)
            except TimeoutError:
                continue  # one last ready() check, then the deadline branch raises
            finally:
                if fut in stream.waiters:
                    stream.waiters.remove(fut)

    async def top_of_book(self, contract: Any) -> tuple[Decimal, Decimal]:
        """Return (bid, ask) from the persistent L1 ticker for *contract*.

        Raises MarketDataTimeoutError when both sides are not valid within
        ``IbConnectionSpec.market_data_timeout``.
        """
        key = _stream_key(contract)
        stream = self._l1_streams.get(key)
        if stream is None:
            self._require_free_line()
            stream = _Stream(contract=contract, hook=lambda _ticker: self._wake_l1(key))
            self._request_persistent(stream, depth=False)
            self._l1_streams[key] = stream
            self._take_line()

        def ready() -> tuple[Decimal, Decimal] | None:
            ticker = stream.ticker
            if ticker is None:
                return None
            bid, ask = ticker.bid, ticker.ask
            # NaN (unset) fails both comparisons; IB also uses -1 for "no value".
            if bid > 0 and ask > 0:
                return Decimal(str(bid)), Decimal(str(ask))
            return None

        result: tuple[Decimal, Decimal] = await self._wait_for(
            stream, ready, "top of book", depth=False
        )
        return result

    def _wake_l1(self, key: Hashable) -> None:
        stream = self._l1_streams.get(key)
        if stream is not None:
            stream.wake()

    async def depth_snapshot(
        self, contract: Any
    ) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        """Return (bids, asks) as (price, size) levels from the shared depth stream.

        Raises MarketDataTimeoutError when no depth arrives within
        ``IbConnectionSpec.market_data_timeout``.
        """
        stream = self._acquire_depth(contract)

        def ready() -> tuple[list, list] | None:
            if stream.bids or stream.asks:
                return list(stream.bids), list(stream.asks)
            return None

        try:
            result: tuple[
                list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]
            ] = await self._wait_for(stream, ready, "depth", depth=True)
            return result
        finally:
            self._release_depth(stream)

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
        return frozenset(self._error_router._stale_farms)

    def subscribe_farm_status(
        self, callback: Callable[[FarmStatusEvent], None]
    ) -> SubscriptionHandle:
        """Observe data-farm status changes; the handle's close() unsubscribes."""
        bus_subscription = self._event_bus.subscribe(FARM_TOPIC, callback)
        return SubscriptionHandle(
            channel=FARM_TOPIC,
            contract=None,
            _cleanup=lambda: self._event_bus.unsubscribe(bus_subscription),
        )

    def _on_farm_status(self, farm: str, ok: bool, code: int, msg: str) -> None:
        """Mirror IB-originated farm status codes (2103..2158) onto the bus."""
        self._event_bus.publish(FARM_TOPIC, FarmStatusEvent(farm, ok, code, msg))

    def cancel_reason(self, order_id: int) -> str | None:
        """Return IB's code-202 cancellation text for *order_id*, or None if unknown."""
        return self._error_router._cancel_reasons.get(order_id)

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
