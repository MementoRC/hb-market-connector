"""KrakenConnectorCore: pure-Python ``ExchangePyBase``-style order core for Kraken.

Mirrors the order API hummingbot strategies use (sync ``buy`` / ``sell`` /
``cancel`` returning a client order id, ``in_flight_orders``, ``fetch_order``,
cache-backed price / order-book / balance reads) on top of
:class:`~market_connector.exchanges.kraken.kraken_gateway.KrakenGateway`, without
importing hummingbot (ADR 0001). The outcome of every order is published as the
plain events in :mod:`market_connector.hb_compat.order_events` on the injected
:class:`~market_connector.hb_compat.event_bus.EventBus`, under hummingbot's
``MarketEvent`` integer ids.

Notes:
    - ``buy`` / ``sell`` / ``cancel`` are sync and must be called from a running event
      loop; the REST call runs in a background task and failures surface as
      ``MarketOrderFailureEvent`` (198), never as exceptions or unhandled-task warnings.
      Amount and price are rounded down to the pair's lot / tick before AddOrder.
    - The wire pair is the ``altname`` from ``AssetPairs`` (hummingbot pair derived from
      ``wsname``); the symbol mapper's ``to_exchange_pair`` is deliberately not used.
    - Each Completed / Cancelled / Failure event is emitted once per order; fills are
      deduplicated by trade id so REST polling and the WS ``ownTrades`` feed can overlap.
      Completion is decided in Decimal (``executed >= amount``) or by Kraken's order status.
    - WS fills for a txid not yet known (AddOrder still in flight) are buffered (60s /
      1000 entries) and booked right after the Created event. Fills reported after an
      order finished are still booked (one 107 each, no second terminal event).
    - ``cancel`` during PENDING_CREATE is remembered and sent once the txid is known; if
      the create fails the order ends with 198 only.
    - Triggered orders (stop-loss / take-profit) report trade ids QueryTrades rejects with
      ``EOrder:Invalid Order ID``; only then is the executed volume booked from the order
      status. Any other QueryTrades error leaves the order open for the next poll.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from data_type_primitives import OrderBookEntry, OrderBookSnapshot, TradingRules
from data_type_primitives.common import OrderType, PositionAction, PriceType, TradeType
from data_type_primitives.in_flight_order import InFlightOrder, OrderState, OrderUpdate

from market_connector.exceptions import ExchangeCancelOnlyError, ExchangeUnavailableError
from market_connector.exchanges.kraken.errors import KrakenAPIError
from market_connector.exchanges.kraken.hb_compat.kraken_core_support import (
    NAN,
    PendingTrades,
    build_pair_tables,
    build_trade_update,
    completed_event,
    created_event,
    filled_event,
    quantize_amount,
    quantize_order,
    round_down,
    update_from_rest,
    update_from_ws,
)
from market_connector.exchanges.kraken.schemas.enums import KrakenOrderState
from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER
from market_connector.hb_compat.logging import get_logger
from market_connector.hb_compat.order_events import (
    MARKET_EVENT_IDS,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from data_type_primitives.in_flight_order import TradeUpdate
    from event_bus import EventKey

    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.exchanges.kraken.schemas.rest import OrderInfo
    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail, OwnTradesEvent
    from market_connector.hb_compat.event_bus import EventBus

logger = get_logger(__name__)

KRAKEN_ORDER_STATE_MAP: Mapping[KrakenOrderState, OrderState] = {
    KrakenOrderState.PENDING: OrderState.PENDING_CREATE,
    KrakenOrderState.OPEN: OrderState.OPEN,
    KrakenOrderState.CLOSED: OrderState.FILLED,
    KrakenOrderState.CANCELED: OrderState.CANCELED,
    KrakenOrderState.EXPIRED: OrderState.CANCELED,
}


_UNRESOLVED: frozenset[OrderState] = frozenset({OrderState.PENDING_CREATE, OrderState.UNKNOWN})
# Kraken userref is a signed int32. The per-order counter only produces non-negative values
# (0..2**31-2); linked-order tags (#240) live in negative space, so they never collide.
_USERREF_MAX = 2**31 - 1
_LIMIT_PRICE_TYPES: frozenset[OrderType] = frozenset(
    {OrderType.STOP_LOSS_LIMIT, OrderType.TAKE_PROFIT_LIMIT, OrderType.TRAILING_STOP_LIMIT}
)


class LostOrderError(Exception):
    """An order whose AddOrder response was lost is on neither OpenOrders nor ClosedOrders."""


_ABANDON_WAIT = 0.05  # final wait for cancelled tasks before stop() abandons stuck ones
_MAX_RETRY_EXPONENT = 30  # keeps the retry backoff finite after many consecutive failures


def _pick_request_match(order: InFlightOrder, found: dict[str, OrderInfo]) -> str | None:
    """The txid, among orders sharing the order's userref, that is the order itself.

    A bracket's released exit inherits the entry's userref but takes the opposite side, so
    only same-side orders qualify; among those the one whose volume equals the request wins,
    then the earliest opened. Returns ``None`` when only foreign orders (e.g. the exit) exist.
    """
    side = order.trade_type.name.lower()
    same_side = {t: i for t, i in found.items() if i.descr.type in ("", side)}
    if not same_side:
        return None

    def rank(txid: str) -> tuple[bool, float]:
        info = same_side[txid]
        return Decimal(info.vol) != order.amount, info.opentm

    return min(same_side, key=rank)


def _pair_set(trading_pairs: Iterable[str]) -> set[str]:
    if isinstance(trading_pairs, str):
        raise TypeError("trading_pairs must be an iterable of pairs, not a str")
    return set(trading_pairs)


def _monotonic() -> float:
    """Default clock; resolves ``time.monotonic`` per call (patchable in tests)."""
    return time.monotonic()


_MAX_FINISHED_ORDERS = 1000
_QUERY_ORDERS_BATCH = 50  # txids per QueryOrders request
_TRIGGERED_ORDER_ERROR = "EOrder:Invalid Order ID"

# Outcome of syncing an order's fills over REST.
_FILLS_OK = "ok"
_FILLS_TRIGGERED = "triggered"  # QueryTrades rejected the ids: a triggered order
_FILLS_ERROR = "error"  # transient failure: retry on the next poll


class KrakenConnectorCore:
    """Order, market-data and balance core for Kraken (see module docstring).

    Args:
        gateway: A started :class:`KrakenGateway`.
        event_bus: Bus the order events are published on.
        status_poll_interval: Seconds between status polls while :meth:`start` is active.
        stop_grace: Seconds :meth:`stop` lets in-flight create/cancel tasks finish before
            cancelling them.
        clock: Monotonic seconds source for the cancel-only backoff window.
        cancel_only_backoff: Initial seconds new orders are failed locally after Kraken
            replies cancel-only; doubles on each further reply, reset by a successful AddOrder.
        cancel_only_backoff_max: Cap for the doubling window.
        unknown_order_grace: Seconds (on ``clock``, from order creation) an UNKNOWN order
            absent from Kraken's open and closed orders stays tracked before it is failed;
            a found order is adopted at once.
        trading_rules_refresh_interval: Seconds between AssetPairs refreshes.
        balance_refresh_interval: Seconds between balance refreshes (None: status poll interval).
        price_refresh_interval: Seconds between ticker refreshes of tracked and in-flight
            pairs (None: status poll interval).
        own_trades_ws: Keep an ownTrades WS subscription alive while started.
        ws_reconnect_delay: Initial backoff before resubscribing; doubles up to
            ``ws_reconnect_delay_max`` and resets after a successful subscription.
        terminal_fill_wait_s: Seconds an order Kraken reports closed/canceled stays in flight
            (polled, no terminal event) while booked volume trails ``vol_exec``; on expiry it
            finishes on booked amounts with a WARNING and ``late_trade_drops`` += 1.
        refresh_retry_delay: Initial wait before retrying a failed periodic refresh; doubles
            per consecutive failure, capped at that refresh's interval.

    Raises:
        ValueError: An interval or delay option is not > 0.
    """

    def __init__(
        self,
        gateway: KrakenGateway,
        *,
        event_bus: EventBus,
        status_poll_interval: float = 10.0,
        stop_grace: float = 5.0,
        clock: Callable[[], float] = _monotonic,
        cancel_only_backoff: float = 10.0,
        cancel_only_backoff_max: float = 300.0,
        unknown_order_grace: float = 0.0,
        trading_rules_refresh_interval: float = 1800.0,
        balance_refresh_interval: float | None = None,
        price_refresh_interval: float | None = None,
        own_trades_ws: bool = True,
        ws_reconnect_delay: float = 1.0,
        ws_reconnect_delay_max: float = 60.0,
        refresh_retry_delay: float = 5.0,
        max_finished_orders: int = _MAX_FINISHED_ORDERS,
        finished_order_grace_s: float = 600.0,
        max_finished_orders_hard: int = 10_000,
        terminal_fill_wait_s: float = 30.0,
    ) -> None:
        if not max_finished_orders_hard >= max_finished_orders >= 1:
            raise ValueError(
                "need max_finished_orders_hard >= max_finished_orders >= 1, got "
                f"{max_finished_orders_hard} / {max_finished_orders}"
            )
        if finished_order_grace_s < 0:
            raise ValueError(f"finished_order_grace_s must be >= 0, got {finished_order_grace_s}")
        self._max_finished = max_finished_orders
        self._finished_grace = finished_order_grace_s
        self._max_finished_hard = max_finished_orders_hard
        self._finished_at: dict[str, float] = {}  # cid -> clock() at finish
        self._evicted_txids: OrderedDict[str, str] = OrderedDict()  # txid -> cid tombstones
        self._late_seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._late_trade_drops = 0
        if terminal_fill_wait_s < 0:
            raise ValueError(f"terminal_fill_wait_s must be >= 0, got {terminal_fill_wait_s}")
        self._terminal_fill_wait = terminal_fill_wait_s
        # cid -> (clock() when Kraken went terminal first, highest-vol_exec terminal status)
        self._held: dict[str, tuple[float, OrderInfo]] = {}
        for name, value in (
            ("status_poll_interval", status_poll_interval),
            ("trading_rules_refresh_interval", trading_rules_refresh_interval),
            ("balance_refresh_interval", balance_refresh_interval),
            ("price_refresh_interval", price_refresh_interval),
            ("ws_reconnect_delay", ws_reconnect_delay),
            ("ws_reconnect_delay_max", ws_reconnect_delay_max),
            ("refresh_retry_delay", refresh_retry_delay),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        self._retry_delay = refresh_retry_delay
        self._rules_interval = trading_rules_refresh_interval
        self._balance_interval = (
            status_poll_interval if balance_refresh_interval is None else balance_refresh_interval
        )
        self._price_interval = (
            status_poll_interval if price_refresh_interval is None else price_refresh_interval
        )
        self._own_trades_ws = own_trades_ws
        self._ws_delay = ws_reconnect_delay
        self._ws_delay_max = ws_reconnect_delay_max
        self._user_stream_up = False
        self._tracked: set[str] = set()
        self._loop_tasks: list[asyncio.Task[None]] = []
        self._clock = clock
        self._unknown_grace = unknown_order_grace
        self._created_at: dict[str, float] = {}  # cid -> clock() at creation
        self._backoff_initial = cancel_only_backoff
        self._backoff_max = cancel_only_backoff_max
        self._backoff_current = cancel_only_backoff
        self._cancel_only_until = 0.0
        self._gateway = gateway
        self._bus = event_bus
        self._poll_interval = status_poll_interval
        self._stop_grace = stop_grace
        self._poll_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.in_flight_orders: dict[str, InFlightOrder] = {}
        self._finished: dict[str, InFlightOrder] = {}
        self._txid_to_cid: dict[str, str] = {}
        # Seeded from the clock (ms) so a restart does not reuse userrefs of still-open orders.
        self._userref_counter = int(time.time() * 1000) % _USERREF_MAX
        self._userrefs: dict[str, int] = {}  # cid -> userref
        self._userref_to_cid: dict[int, str] = {}  # write-only; kept for diagnostics
        self._limit_prices: dict[str, tuple[Decimal | None, bool]] = {}  # cid -> (limit, pct)
        self._submitting: set[str] = set()  # AddOrder request in flight
        self._cancel_requested: set[str] = set()  # cancel asked before the txid was known
        self._cancelling: set[str] = set()  # CancelOrder in flight
        self._syncing_fills: set[str] = set()  # QueryTrades in flight
        self._pending_trades = PendingTrades(on_drop=self._on_pending_drop)
        self.trading_rules: dict[str, TradingRules] = {}
        self._wire_pairs: dict[str, str] = {}
        self._prices: dict[str, dict[PriceType, Decimal]] = {}
        self._books: dict[str, OrderBookSnapshot] = {}
        self._balances: dict[str, Decimal] = {}
        self._available: dict[str, Decimal] = {}
        self._rules_loaded = False
        self._balances_loaded = False

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    @property
    def status_dict(self) -> dict[str, bool]:
        """Fresh readiness map; keys and triggers:

        * ``symbols_mapping_initialized``: True after a successful ``update_trading_rules``.
        * ``trading_rule_initialized``: same trigger.
        * ``account_balance``: True after a successful ``update_balances``.
        * ``status_polling_initialized``: True only while the ``start()`` poller is live.
        * ``user_stream_initialized``: only with ``own_trades_ws``; True while the ownTrades
          subscription is up (False during a drop / backoff and after ``stop()``).

        Rules and balances flags survive ``stop()`` and failed refreshes (the cache stays).
        The adapter maps these keys onto its own status dict. There is deliberately no
        order-book key: order books are cached on demand per pair.
        """
        polling = self._poll_task is not None and not self._poll_task.done()
        status = {
            "symbols_mapping_initialized": self._rules_loaded,
            "trading_rule_initialized": self._rules_loaded,
            "account_balance": self._balances_loaded,
            "status_polling_initialized": polling,
        }
        if self._own_trades_ws:
            status["user_stream_initialized"] = self._user_stream_up
        return status

    @property
    def ready(self) -> bool:
        """``True`` when every :attr:`status_dict` entry is ``True``."""
        return all(self.status_dict.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the self-driving loops: refreshes, status poll, ownTrades WS (idempotent).

        Rules, balances and prices refresh at once and then every interval; a failed
        refresh is logged and retried on the next tick.
        """
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
        if any(not t.done() for t in self._loop_tasks):
            return
        loop = asyncio.get_running_loop()
        self._loop_tasks = [
            loop.create_task(self._periodic(self.update_trading_rules, self._rules_interval)),
            loop.create_task(self._periodic(self.update_balances, self._balance_interval)),
            loop.create_task(self._periodic(self._refresh_prices, self._price_interval)),
        ]
        if self._own_trades_ws:
            self._loop_tasks.append(loop.create_task(self._own_trades_loop()))

    async def stop(self) -> None:
        """Stop all loops and drain background tasks; none outlives the call (idempotent).

        Loops and the ownTrades subscription are cancelled (waiting at most ``stop_grace``).
        In-flight create/cancel tasks get ``stop_grace`` seconds to finish, the rest is
        cancelled. A create cancelled mid-AddOrder stays tracked: its outcome is unknown.
        """
        task, self._poll_task = self._poll_task, None
        loops, self._loop_tasks = self._loop_tasks, []
        stopping = [t for t in (task, *loops) if t is not None]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_grace
        for t in stopping:
            t.cancel()
        while (live := self._live_tasks() | {t for t in stopping if not t.done()}) and (
            remaining := deadline - loop.time()
        ) > 0:
            await asyncio.wait(live, timeout=remaining)
        self._user_stream_up = False
        leftover = self._live_tasks() | {t for t in stopping if not t.done()}
        for pending in leftover:
            pending.cancel()
        if leftover:
            _, stuck = await asyncio.wait(leftover, timeout=_ABANDON_WAIT)
            if stuck:
                logger.warning("stop(): abandoning %d task(s) that ignore cancellation", len(stuck))

    def _live_tasks(self) -> set[asyncio.Task[Any]]:
        return {t for t in self._tasks if not t.done()}

    async def _periodic(self, refresh: Callable[[], Awaitable[None]], interval: float) -> None:
        failures = 0
        while True:
            try:
                await refresh()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                failures += 1
                logger.warning("periodic refresh %s failed", refresh.__name__, exc_info=True)
            if failures:
                backoff = self._retry_delay * 2 ** min(failures - 1, _MAX_RETRY_EXPONENT)
                await asyncio.sleep(min(backoff, interval))
            else:
                await asyncio.sleep(interval)

    async def _refresh_prices(self) -> None:
        pairs = self._tracked | {o.trading_pair for o in self.in_flight_orders.values()}
        await self.update_prices(sorted(pairs))

    async def _own_trades_loop(self) -> None:
        """Keep the ownTrades subscription alive: resubscribe after a drop, with backoff."""
        delay = self._ws_delay
        while True:
            try:
                generation = self._gateway.ws_generation  # before subscribing: a drop is not missed
                async with await self._gateway.subscribe_own_trades(self.on_own_trades):
                    self._user_stream_up = True
                    delay = self._ws_delay
                    await self._gateway.wait_ws_disconnect(generation)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("ownTrades subscription failed", exc_info=True)
            finally:
                self._user_stream_up = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._ws_delay_max)

    def track_pairs(self, trading_pairs: Iterable[str]) -> None:
        """Include ``trading_pairs`` in the periodic price refresh (a bare ``str``: TypeError)."""
        self._tracked |= _pair_set(trading_pairs)

    def untrack_pairs(self, trading_pairs: Iterable[str]) -> None:
        """Drop ``trading_pairs`` from the periodic price refresh (unknown pairs ignored)."""
        self._tracked -= _pair_set(trading_pairs)

    @property
    def tracked_pairs(self) -> frozenset[str]:
        return frozenset(self._tracked)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001
                logger.warning("status poll failed", exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        await self._resolve_unknown_orders()
        orders = [o for o in self.in_flight_orders.values() if o.exchange_order_id]
        for start in range(0, len(orders), _QUERY_ORDERS_BATCH):
            await self._poll_batch(orders[start : start + _QUERY_ORDERS_BATCH])

    # ------------------------------------------------------------------
    # Order API (sync, background REST)
    # ------------------------------------------------------------------

    def buy(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = NAN,
        *,
        limit_price: Decimal | None = None,
        price_in_percent: bool = False,
        position_action: PositionAction | None = None,
        expiration_ts: float | None = None,
    ) -> str:
        """Track a BUY order and send it in the background; returns the client order id.

        ``limit_price`` is Kraken's ``price2`` (required for ``*_LIMIT`` types, else
        ``ValueError``). ``position_action`` (a ``PositionAction`` or None, else
        ``TypeError``) and ``expiration_ts`` are accepted for ``ExchangePyBase`` parity
        and not forwarded; any other keyword raises ``TypeError``.
        """
        return self._create(
            TradeType.BUY, trading_pair, amount, order_type, price,
            limit_price, price_in_percent, position_action,
        )  # fmt: skip

    def sell(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = NAN,
        *,
        limit_price: Decimal | None = None,
        price_in_percent: bool = False,
        position_action: PositionAction | None = None,
        expiration_ts: float | None = None,
    ) -> str:
        """Track a SELL order and send it in the background; returns the client order id."""
        return self._create(
            TradeType.SELL, trading_pair, amount, order_type, price,
            limit_price, price_in_percent, position_action,
        )  # fmt: skip

    def cancel(self, trading_pair: str, client_order_id: str) -> str:
        """Cancel in the background; returns ``client_order_id``. Unknown ids are a no-op.

        Before the txid is known the request is remembered and sent once AddOrder returns.
        """
        asyncio.get_running_loop()  # fail fast outside a running loop
        order = self.in_flight_orders.get(client_order_id)
        if order is None:
            logger.warning("cancel: unknown or finished order %s", client_order_id)
        elif client_order_id in self._held:
            logger.debug("cancel: %s already terminal on Kraken, awaiting fills", client_order_id)
        elif client_order_id in self._cancel_requested | self._cancelling:
            logger.debug("cancel: already requested for %s", client_order_id)
        elif order.exchange_order_id is None:
            self._cancel_requested.add(client_order_id)
        else:
            self._start_cancel(order)
        return client_order_id

    def _start_cancel(self, order: InFlightOrder) -> None:
        self._cancelling.add(order.client_order_id)
        self._track(asyncio.get_running_loop().create_task(self._cancel_order(order)))

    def fetch_order(self, client_order_id: str) -> InFlightOrder | None:
        """The order, in flight or finished (finished ones have ``is_open`` False)."""
        return self.in_flight_orders.get(client_order_id) or self._finished.get(client_order_id)

    def client_order_id_for_txid(self, txid: str) -> str | None:
        return self._txid_to_cid.get(txid)

    def txid_for_client_order_id(self, client_order_id: str) -> str | None:
        order = self.fetch_order(client_order_id)
        return None if order is None else order.exchange_order_id

    def _create(
        self,
        trade_type: TradeType,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price: Decimal,
        limit_price: Decimal | None,
        price_in_percent: bool,
        position_action: PositionAction | None,
    ) -> str:
        loop = asyncio.get_running_loop()
        if position_action is not None and not isinstance(position_action, PositionAction):
            raise TypeError(f"position_action must be a PositionAction, got {position_action!r}")
        if order_type in _LIMIT_PRICE_TYPES and limit_price is None:
            raise ValueError(f"{order_type.name} requires limit_price")
        amount, price = quantize_order(self.trading_rules.get(trading_pair), amount, price)
        client_order_id = f"hbot-{trade_type.name.lower()}-{uuid.uuid4().hex}"
        self._assign_userref(client_order_id)
        self._limit_prices[client_order_id] = (limit_price, price_in_percent)
        self._created_at[client_order_id] = self._clock()
        order = InFlightOrder(
            client_order_id=client_order_id,
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            amount=amount,
            creation_timestamp=time.time(),
            price=price,
        )
        self.in_flight_orders[client_order_id] = order
        self._track(loop.create_task(self._submit_order(order)))
        return client_order_id

    def _assign_userref(self, client_order_id: str) -> None:
        """Give the order the next int32 ``userref`` (1..2**31-2, wrapping)."""
        self._userref_counter = self._userref_counter % _USERREF_MAX + 1
        self._userrefs[client_order_id] = self._userref_counter
        self._userref_to_cid[self._userref_counter] = client_order_id

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background order task failed", exc_info=exc)

    async def _submit_order(self, order: InFlightOrder) -> None:
        cid = order.client_order_id
        if self._clock() < self._cancel_only_until:
            self._fail(order, ExchangeCancelOnlyError("cancel_only backoff window open"))
            return
        self._submitting.add(cid)
        limit_price, price_in_percent = self._limit_prices.get(cid, (None, False))
        try:
            wire_pair = self._wire_pair(order.trading_pair)
            price = None if order.order_type is OrderType.MARKET else order.price
            if price is not None and price.is_nan():
                price = None
            txid = await self._gateway._place_order_compat(
                trading_pair=wire_pair,
                order_type=order.order_type.name,  # the gateway maps order types by name
                side=order.trade_type.name.lower(),
                amount=order.amount,
                price=price,
                userref=self._userrefs.get(cid),
                limit_price=limit_price,
                price_in_percent=price_in_percent,
            )
        except asyncio.CancelledError:
            logger.warning(
                "create of %s cancelled mid-flight; outcome unknown, order stays tracked",
                order.client_order_id,
            )
            raise
        except (httpx.TimeoutException, httpx.TransportError, ExchangeUnavailableError) as exc:
            self._mark_unknown(order, exc)
            return
        except ExchangeCancelOnlyError as exc:
            self._open_cancel_only_window()
            self._fail(order, exc)
            return
        except Exception as exc:  # noqa: BLE001
            if order.exchange_order_id is not None:
                logger.warning(
                    "AddOrder for %s raised (%s) but the order was already adopted as %s; "
                    "not failing it",
                    cid,
                    exc,
                    order.exchange_order_id,
                )
                return
            self._fail(order, exc)
            return
        finally:
            self._submitting.discard(cid)
        self._backoff_current = self._backoff_initial
        self._on_placed(order, txid)

    def _open_cancel_only_window(self) -> None:
        """Fail new orders locally for the current window, then double it up to the cap."""
        self._cancel_only_until = self._clock() + self._backoff_current
        self._backoff_current = min(self._backoff_current * 2, self._backoff_max)

    def _mark_unknown(self, order: InFlightOrder, exc: Exception) -> None:
        """AddOrder's outcome is unknown (lost response): keep tracking, resolve by userref."""
        cid = order.client_order_id
        if order.exchange_order_id is not None or cid not in self.in_flight_orders:
            return  # already adopted by the resolver, or finished
        logger.warning(
            "AddOrder for %s got no response (%s); outcome unknown, will resolve by userref",
            order.client_order_id,
            exc,
        )
        order.current_state = OrderState.UNKNOWN

    async def _resolve_unknown_orders(self) -> None:
        """Adopt or fail every UNKNOWN / txid-less PENDING_CREATE order via its userref."""
        for order in list(self.in_flight_orders.values()):
            if order.exchange_order_id is not None or order.current_state not in _UNRESOLVED:
                continue
            try:
                await self._resolve_one(order)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("userref lookup failed for %s", order.client_order_id, exc_info=True)

    async def _resolve_one(self, order: InFlightOrder) -> None:
        cid = order.client_order_id
        userref = self._userrefs.get(cid)
        if userref is None:
            return
        found = _pick_request_match(
            order, await self._gateway.get_open_orders(userref=userref)
        ) or _pick_request_match(order, await self._gateway.get_closed_orders(userref=userref))
        # The lookups awaited: AddOrder may have completed or the order been finished meanwhile.
        if order.exchange_order_id is not None or cid not in self.in_flight_orders:
            return
        if found:
            self._on_placed(order, found)
        elif cid in self._submitting:  # a live AddOrder request may still land
            return
        elif self._clock() - self._created_at.get(cid, float("-inf")) >= self._unknown_grace:
            self._fail(order, LostOrderError(f"order {cid} (userref {userref}) not on Kraken"))

    async def _cancel_order(self, order: InFlightOrder) -> None:
        txid = order.exchange_order_id
        if txid is None:
            return
        reconciled = True
        try:
            cancelled = await self._gateway.cancel_order(
                trading_pair=self._wire_pair(order.trading_pair), txid=txid
            )
            if cancelled:
                reconciled = await self._sync_before_cancel(order, txid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("cancel failed for %s", order.client_order_id, exc_info=True)
            return
        finally:
            self._cancelling.discard(order.client_order_id)
        if cancelled and reconciled:
            self._cancelled(order)

    async def _sync_before_cancel(self, order: InFlightOrder, txid: str) -> bool:
        """Book fills the cancel ack raced with, so they precede the 106.

        Returns ``False`` when fills may still be unbooked: the order then stays in flight
        for the status poll (held, up to ``terminal_fill_wait_s``). Skipped while a status
        poll is already fetching this order's trades: that poll books them (as late fills).
        """
        cid = order.client_order_id
        if cid in self._syncing_fills:
            return True
        info = (await self._gateway.query_orders([txid])).get(txid)
        if info is None:
            return True
        outcome = await self._sync_fills(order, info)
        if outcome == _FILLS_TRIGGERED:
            self._synthesize_missing_fill(order, info)
            return True
        if outcome == _FILLS_OK and order.executed_amount_base >= Decimal(info.vol_exec):
            return True
        if cid in self.in_flight_orders:
            since = self._held[cid][0] if cid in self._held else self._clock()
            canceled = info.model_copy(update={"status": KrakenOrderState.CANCELED.value})
            self._held[cid] = (since, canceled)
        return False

    def _on_placed(self, order: InFlightOrder, txid: str) -> None:
        cid = order.client_order_id
        if order.exchange_order_id is not None:
            return  # already adopted by the userref resolver
        now = time.time()
        self._txid_to_cid[txid] = cid
        self._set_state(order, OrderState.OPEN, now, txid)
        self._emit(created_event(order, txid, now))
        for trade_id, detail in self._pending_trades.pop_for(txid, self._clock()):
            self._apply_ws_trade(order, trade_id, detail)
        if cid in self._cancel_requested and cid in self.in_flight_orders:
            self._cancel_requested.discard(cid)
            self._start_cancel(order)

    def _wire_pair(self, trading_pair: str) -> str:
        try:
            return self._wire_pairs[trading_pair]
        except KeyError:
            raise ValueError(
                f"unknown trading pair {trading_pair!r}; trading rules not loaded or pair absent"
            ) from None

    # ------------------------------------------------------------------
    # Order state and events
    # ------------------------------------------------------------------

    def _emit(self, event: object) -> None:
        key: EventKey = MARKET_EVENT_IDS[type(event).__name__]
        self._bus.publish(key, event)

    def _set_state(
        self, order: InFlightOrder, state: OrderState, timestamp: float, txid: str | None = None
    ) -> None:
        order.update_with_order_update(
            OrderUpdate(
                trading_pair=order.trading_pair,
                update_timestamp=timestamp,
                new_state=state,
                client_order_id=order.client_order_id,
                exchange_order_id=txid,
            )
        )

    def _finish(self, order: InFlightOrder, state: OrderState) -> bool:
        """Move ``order`` out of ``in_flight_orders``; ``False`` if it was already finished."""
        cid = order.client_order_id
        if self.in_flight_orders.pop(cid, None) is None:
            return False
        self._cancel_requested.discard(cid)
        self._held.pop(cid, None)
        self._userref_to_cid.pop(self._userrefs.pop(cid, 0), None)
        self._limit_prices.pop(cid, None)
        self._created_at.pop(cid, None)
        self._set_state(order, state, time.time())
        now = self._clock()
        self._finished[cid] = order
        self._finished_at[cid] = now
        while len(self._finished) > self._max_finished:
            oldest = next(iter(self._finished))
            over_hard = len(self._finished) > self._max_finished_hard
            if not over_hard and now - self._finished_at[oldest] < self._finished_grace:
                break
            if over_hard and now - self._finished_at[oldest] < self._finished_grace:
                logger.warning(
                    "evicting finished order %s inside its grace period (hard cap %d reached)",
                    oldest,
                    self._max_finished_hard,
                )
            self._forget(self._finished.pop(oldest))
        return True

    def _forget(self, order: InFlightOrder) -> None:
        cid = order.client_order_id
        self._held.pop(cid, None)
        self._finished_at.pop(cid, None)
        txid = order.exchange_order_id
        if txid is not None:
            self._txid_to_cid.pop(txid, None)
            self._evicted_txids[txid] = cid
            while len(self._evicted_txids) > self._max_finished_hard:
                self._evicted_txids.popitem(last=False)

    @property
    def late_trade_drops(self) -> int:
        """Late WS trades that could not be booked (evicted order or expired buffer)."""
        return self._late_trade_drops

    def _on_pending_drop(self, key: tuple[str, str], detail: OwnTradeDetail, reason: str) -> None:
        self._late_trade_drops += 1
        logger.warning(
            "buffered trade %s for unmatched order %s dropped (%s); not booked",
            key[1],
            detail.ordertxid,
            reason,
        )

    def _note_evicted_trade(self, trade_id: str, detail: OwnTradeDetail) -> None:
        txid = detail.ordertxid
        key = (txid, trade_id)
        if key in self._late_seen:
            return
        self._late_seen[key] = None
        while len(self._late_seen) > self._max_finished_hard:
            self._late_seen.popitem(last=False)
        self._late_trade_drops += 1
        logger.warning(
            "late trade for evicted order %s (trade %s, client order %s) not booked",
            txid,
            trade_id,
            self._evicted_txids[txid],
        )

    def _fail(self, order: InFlightOrder, exc: Exception) -> None:
        if not self._finish(order, OrderState.FAILED):
            return
        logger.warning("order %s failed: %s", order.client_order_id, exc)
        self._emit(
            MarketOrderFailureEvent(
                timestamp=time.time(),
                order_id=order.client_order_id,
                order_type=order.order_type,
                error_message=str(exc),
                error_type=type(exc).__name__,
            )
        )

    def _cancelled(self, order: InFlightOrder) -> None:
        if self._finish(order, OrderState.CANCELED):
            self._emit(
                OrderCancelledEvent(
                    timestamp=time.time(),
                    order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                )
            )

    def _complete(self, order: InFlightOrder) -> None:
        if self._finish(order, OrderState.FILLED):
            self._emit(completed_event(order, time.time()))

    def _record_fill(self, order: InFlightOrder, update: TradeUpdate) -> None:
        """Apply a fill once (deduplicated by trade id) and publish ``OrderFilledEvent``.

        Once the executed volume was booked from the order status (``<txid>:status``),
        the real trades describe the same execution and are ignored.
        """
        status_id = f"{order.exchange_order_id}:status"
        if status_id in order.order_fills and update.trade_id != status_id:
            logger.debug("ignoring trade %s after status-derived fill", update.trade_id)
            return
        if order.update_with_trade_update(update):
            self._emit(filled_event(order, update))

    # ------------------------------------------------------------------
    # Fills: WS ownTrades and REST status polling
    # ------------------------------------------------------------------

    def on_own_trades(self, event: OwnTradesEvent) -> None:
        """Sync WS handler: book each trade on its order, buffer those of unknown txids."""
        for trade_id, detail in event.trades.items():
            cid = self._txid_to_cid.get(detail.ordertxid)
            order = self.fetch_order(cid) if cid else None
            if order is not None:
                self._apply_ws_trade(order, trade_id, detail)
            elif detail.ordertxid in self._evicted_txids:
                self._note_evicted_trade(trade_id, detail)
            else:
                self._pending_trades.add(trade_id, detail, self._clock())

    def _apply_ws_trade(self, order: InFlightOrder, trade_id: str, detail: OwnTradeDetail) -> None:
        self._record_fill(order, update_from_ws(order, trade_id, detail))
        if order.executed_amount_base >= order.amount:  # Decimal; no-op once finished
            self._complete(order)
        elif order.client_order_id in self.in_flight_orders and order.executed_amount_base > 0:
            self._set_state(order, OrderState.PARTIALLY_FILLED, time.time())

    async def update_order_status(self, client_order_id: str) -> None:
        """REST QueryOrders then QueryTrades; the order status drives completion."""
        order = self.in_flight_orders.get(client_order_id)
        txid = None if order is None else order.exchange_order_id
        if order is None or txid is None:
            return
        info = (await self._gateway.query_orders([txid])).get(txid)
        if info is not None:
            await self._process_info(order, info)

    async def _process_info(self, order: InFlightOrder, info: OrderInfo) -> None:
        cid = order.client_order_id
        if cid not in self.in_flight_orders or cid in self._syncing_fills:
            return  # an overlapping processor is mid-QueryTrades; it applies its own status
        outcome = await self._sync_fills(order, info)
        if cid not in self.in_flight_orders:
            return
        if outcome == _FILLS_ERROR:
            self._expire_held(order)
        else:
            self._apply_status(order, info, triggered=outcome == _FILLS_TRIGGERED)

    def _expire_held(self, order: InFlightOrder) -> None:
        """Finish a held order whose wait ran out although Kraken's status is unavailable."""
        held = self._held.get(order.client_order_id)
        if held is None or self._clock() - held[0] < self._terminal_fill_wait:
            return
        info = held[1]
        self._apply_terminal(order, info, KRAKEN_ORDER_STATE_MAP[KrakenOrderState(info.status)])

    async def _poll_batch(self, orders: list[InFlightOrder]) -> None:
        txids = [o.exchange_order_id for o in orders if o.exchange_order_id]
        try:
            infos = await self._gateway.query_orders(txids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("batched status poll failed", exc_info=True)
            for order in orders:
                self._expire_held(order)
            return
        for order in orders:
            info = infos.get(order.exchange_order_id or "")
            if info is None:
                if order.client_order_id in self.in_flight_orders:
                    self._expire_held(order)
                continue
            try:
                await self._process_info(order, info)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("status poll failed for %s", order.client_order_id, exc_info=True)

    async def _sync_fills(self, order: InFlightOrder, info: OrderInfo) -> str:
        trade_ids = [t for t in info.trades if t not in order.order_fills]
        if not trade_ids:
            return _FILLS_OK
        self._syncing_fills.add(order.client_order_id)
        try:
            trades = await self._gateway.query_trades(trade_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, KrakenAPIError) and _TRIGGERED_ORDER_ERROR in exc.error_codes:
                return _FILLS_TRIGGERED
            logger.warning("query_trades failed for %s: %s", order.client_order_id, exc)
            return _FILLS_ERROR
        finally:
            self._syncing_fills.discard(order.client_order_id)
        for trade_id, trade in trades.items():
            self._record_fill(order, update_from_rest(order, trade_id, trade))
        return _FILLS_OK

    def _apply_status(self, order: InFlightOrder, info: OrderInfo, *, triggered: bool) -> None:
        try:
            state = KRAKEN_ORDER_STATE_MAP[KrakenOrderState(info.status)]
        except ValueError:
            logger.warning("unknown Kraken order status %r", info.status)
            return
        if triggered and state in {OrderState.FILLED, OrderState.CANCELED}:
            self._synthesize_missing_fill(order, info)
        if state in {OrderState.FILLED, OrderState.CANCELED}:
            self._apply_terminal(order, info, state)
        elif state is OrderState.OPEN:
            partial = order.executed_amount_base > 0
            self._set_state(
                order,
                OrderState.PARTIALLY_FILLED if partial else OrderState.OPEN,
                time.time(),
            )

    def _apply_terminal(self, order: InFlightOrder, info: OrderInfo, state: OrderState) -> None:
        if not self._fills_reconciled(order, info):
            return
        if state is OrderState.FILLED and (
            Decimal(info.vol_exec) >= order.amount or order.executed_amount_base >= order.amount
        ):
            self._complete(order)
        else:  # closed under-filled = cancelled
            self._cancelled(order)

    def _fills_reconciled(self, order: InFlightOrder, info: OrderInfo) -> bool:
        """Whether the terminal event may go out: fills first, terminal last.

        While booked volume trails Kraken's ``vol_exec`` the order is held (kept in flight and
        polled) for up to ``terminal_fill_wait_s``; on expiry it finishes on booked amounts.
        """
        cid = order.client_order_id
        held = self._held.get(cid)
        # an out-of-order (older) status must not lower the volume already seen
        if held is not None and Decimal(held[1].vol_exec) > Decimal(info.vol_exec):
            info = held[1]
        vol_exec = Decimal(info.vol_exec)
        if order.executed_amount_base >= vol_exec:
            return True
        now = self._clock()
        since = held[0] if held is not None else now
        if now - since < self._terminal_fill_wait:
            self._held[cid] = (since, info)
            return False
        self._late_trade_drops += 1
        logger.warning(
            "order %s finished with unreconciled fills (booked %s of vol_exec %s); "
            "missing trades not booked",
            order.exchange_order_id,
            order.executed_amount_base,
            vol_exec,
        )
        return True

    def _synthesize_missing_fill(self, order: InFlightOrder, info: OrderInfo) -> None:
        """Book the executed volume QueryTrades refused to report (triggered orders)."""
        missing = Decimal(info.vol_exec) - order.executed_amount_base
        if missing <= 0:
            return
        cost = max(Decimal(info.cost) - order.executed_amount_quote, Decimal(0))
        fees_booked = sum(
            (f.fee.flat_fees[0].amount for f in order.order_fills.values()), Decimal(0)
        )
        self._record_fill(
            order,
            build_trade_update(
                order,
                trade_id=f"{order.exchange_order_id}:status",
                price=cost / missing if cost > 0 else Decimal(info.price),
                amount=missing,
                cost=cost,
                fee=max(Decimal(info.fee) - fees_booked, Decimal(0)),
                timestamp=time.time(),
            ),
        )

    # ------------------------------------------------------------------
    # Market data (sync cache reads, async refresh)
    # ------------------------------------------------------------------

    async def update_prices(self, trading_pairs: Iterable[str]) -> None:
        """Refresh the ticker cache for ``trading_pairs`` (errors are logged per pair)."""
        for pair in trading_pairs:
            try:
                ticker = await self._gateway.get_ticker(self._wire_pair(pair))
                bid, ask, last = (Decimal(ticker[k][0]) for k in ("b", "a", "c"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("price refresh failed for %s", pair, exc_info=True)
                continue
            self._prices[pair] = {
                PriceType.BestBid: bid,
                PriceType.BestAsk: ask,
                PriceType.MidPrice: (bid + ask) / 2,
                PriceType.LastTrade: last,
            }

    def get_price_by_type(self, trading_pair: str, price_type: PriceType) -> Decimal:
        """Cached price; NaN when unknown, empty or of an unsupported type."""
        return self._prices.get(trading_pair, {}).get(price_type, NAN)

    async def update_order_book(self, trading_pair: str) -> None:
        """Fetch a fresh order-book snapshot into the cache."""
        book = await self._gateway.get_orderbook(self._wire_pair(trading_pair))
        self._books[trading_pair] = OrderBookSnapshot(
            timestamp=int(book.timestamp * 1000),
            bids=sorted(
                (OrderBookEntry(price=p, quantity=q) for p, q in book.bids),
                key=lambda e: e.price,
                reverse=True,
            ),
            asks=sorted(
                (OrderBookEntry(price=p, quantity=q) for p, q in book.asks),
                key=lambda e: e.price,
            ),
        )

    def get_order_book(self, trading_pair: str) -> OrderBookSnapshot | None:
        """Last fetched snapshot, ``None`` before the first :meth:`update_order_book`."""
        return self._books.get(trading_pair)

    # ------------------------------------------------------------------
    # Trading rules and balances
    # ------------------------------------------------------------------

    async def update_trading_rules(self) -> None:
        """Load AssetPairs: trading rules and the hummingbot pair -> wire pair map."""
        rules, self._wire_pairs = build_pair_tables(await self._gateway.get_asset_pairs())
        self.trading_rules = {
            pair: TradingRules(
                trading_pair=pair,
                min_order_size=rule.min_order_size,
                min_notional_size=rule.min_notional_size,
                min_price_increment=Decimal(10) ** -rule.price_decimals,
                min_base_amount_increment=Decimal(10) ** -rule.amount_decimals,
            )
            for pair, rule in rules.items()
        }
        self._rules_loaded = True

    def quantize_order_amount(self, trading_pair: str, amount: Decimal) -> Decimal:
        """Round down to the lot size; 0 if NaN, below the minimum order size or pair unknown.

        A zero size increment means no constraint (amount unchanged above the minimum).
        """
        return quantize_amount(self.trading_rules.get(trading_pair), amount)

    def quantize_order_price(self, trading_pair: str, price: Decimal) -> Decimal:
        """Round down to the price tick; unchanged for an unknown pair or a zero tick."""
        rule = self.trading_rules.get(trading_pair)
        return price if rule is None else round_down(price, rule.min_price_increment)

    def get_order_price_quantum(self, trading_pair: str, price: Decimal) -> Decimal:
        """The price tick of the pair (0 when unknown or unconstrained)."""
        rule = self.trading_rules.get(trading_pair)
        return Decimal(0) if rule is None else rule.min_price_increment

    def get_order_size_quantum(self, trading_pair: str, amount: Decimal) -> Decimal:
        """The lot size of the pair (0 when unknown or unconstrained)."""
        rule = self.trading_rules.get(trading_pair)
        return Decimal(0) if rule is None else rule.min_base_amount_increment

    async def update_balances(self) -> None:
        """Refresh balances from BalanceEx (hummingbot asset names; available = - hold_trade)."""
        balances: dict[str, Decimal] = {}
        available: dict[str, Decimal] = {}
        for code, entry in (await self._gateway.get_balances_ex()).items():
            asset = KRAKEN_SYMBOL_MAPPER.from_exchange_asset(code)
            total = Decimal(entry.balance)
            balances[asset] = balances.get(asset, Decimal(0)) + total
            available[asset] = available.get(asset, Decimal(0)) + total - Decimal(entry.hold_trade)
        self._balances, self._available = balances, available
        self._balances_loaded = True

    def get_balance(self, asset: str) -> Decimal:
        return self._balances.get(asset, Decimal(0))

    def get_available_balance(self, asset: str) -> Decimal:
        return self._available.get(asset, Decimal(0))


__all__ = [
    "KRAKEN_ORDER_STATE_MAP",
    "KrakenConnectorCore",
]
