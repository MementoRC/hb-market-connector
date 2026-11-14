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
import contextlib
import time
import uuid
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx
from data_type_primitives.common import OrderType, PriceType, TradeType
from data_type_primitives.in_flight_order import InFlightOrder, OrderState, OrderUpdate

from market_connector.exchanges.kraken.errors import KrakenAPIError
from market_connector.exchanges.kraken.hb_compat.kraken_core_support import (
    NAN,
    PendingTrades,
    TradingRule,
    build_pair_tables,
    build_trade_update,
    completed_event,
    created_event,
    filled_event,
    quantize_amount,
    quantize_order,
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
    from collections.abc import Iterable, Mapping

    from data_type_primitives.in_flight_order import TradeUpdate

    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.exchanges.kraken.schemas.rest import OrderInfo
    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail, OwnTradesEvent
    from market_connector.hb_compat.event_bus import EventBus
    from market_connector.primitives import OrderBookSnapshot

logger = get_logger(__name__)

KRAKEN_ORDER_STATE_MAP: Mapping[KrakenOrderState, OrderState] = {
    KrakenOrderState.PENDING: OrderState.PENDING_CREATE,
    KrakenOrderState.OPEN: OrderState.OPEN,
    KrakenOrderState.CLOSED: OrderState.FILLED,
    KrakenOrderState.CANCELED: OrderState.CANCELED,
    KrakenOrderState.EXPIRED: OrderState.CANCELED,
}


class _ExtraOrderState(Enum):
    """States ``data_type_primitives.OrderState`` lacks (``.value`` above its highest, 10)."""

    UNKNOWN = 11  # AddOrder response lost: the order may or may not be on the exchange


UNKNOWN_STATE = _ExtraOrderState.UNKNOWN
_UNRESOLVED: frozenset[Any] = frozenset({OrderState.PENDING_CREATE, UNKNOWN_STATE})
_USERREF_MAX = 2**31 - 1  # Kraken userref is a signed int32


class LostOrderError(Exception):
    """An order whose AddOrder response was lost is on neither OpenOrders nor ClosedOrders."""


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
    """

    def __init__(
        self,
        gateway: KrakenGateway,
        *,
        event_bus: EventBus,
        status_poll_interval: float = 10.0,
        stop_grace: float = 5.0,
    ) -> None:
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
        self._userref_to_cid: dict[int, str] = {}
        self._submitting: set[str] = set()  # AddOrder request in flight
        self._cancel_requested: set[str] = set()  # cancel asked before the txid was known
        self._cancelling: set[str] = set()  # CancelOrder in flight
        self._syncing_fills: set[str] = set()  # QueryTrades in flight
        self._pending_trades = PendingTrades()
        self.trading_rules: dict[str, TradingRule] = {}
        self._wire_pairs: dict[str, str] = {}
        self._prices: dict[str, dict[PriceType, Decimal]] = {}
        self._books: dict[str, OrderBookSnapshot] = {}
        self._balances: dict[str, Decimal] = {}
        self._available: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the status-polling task (idempotent)."""
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling and drain background tasks; none outlives the call (idempotent).

        In-flight create/cancel tasks get ``stop_grace`` seconds to finish, the rest is
        cancelled. A create cancelled mid-AddOrder stays tracked: its outcome is unknown.
        """
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_grace
        while (live := self._live_tasks()) and (remaining := deadline - loop.time()) > 0:
            await asyncio.wait(live, timeout=remaining)
        leftover = self._live_tasks()
        for pending in leftover:
            pending.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)

    def _live_tasks(self) -> set[asyncio.Task[Any]]:
        return {t for t in self._tasks if not t.done()}

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_once()
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
        **kwargs: Any,
    ) -> str:
        """Track a BUY order and send it in the background; returns the client order id.

        ``kwargs`` (``expiration_ts``, ``position_action``) are accepted for
        ``ExchangePyBase`` parity and not forwarded.
        """
        return self._create(TradeType.BUY, trading_pair, amount, order_type, price)

    def sell(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = NAN,
        **kwargs: Any,
    ) -> str:
        """Track a SELL order and send it in the background; returns the client order id."""
        return self._create(TradeType.SELL, trading_pair, amount, order_type, price)

    def cancel(self, trading_pair: str, client_order_id: str) -> str:
        """Cancel in the background; returns ``client_order_id``. Unknown ids are a no-op.

        Before the txid is known the request is remembered and sent once AddOrder returns.
        """
        asyncio.get_running_loop()  # fail fast outside a running loop
        order = self.in_flight_orders.get(client_order_id)
        if order is None:
            logger.warning("cancel: unknown or finished order %s", client_order_id)
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
    ) -> str:
        loop = asyncio.get_running_loop()
        amount, price = quantize_order(self.trading_rules.get(trading_pair), amount, price)
        client_order_id = f"hbot-{trade_type.name.lower()}-{uuid.uuid4().hex}"
        self._assign_userref(client_order_id)
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
        """Give the order the next int32 ``userref`` (1..2**31-1, wrapping)."""
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
        self._submitting.add(cid)
        try:
            wire_pair = self._wire_pair(order.trading_pair)
            price = None if order.order_type is OrderType.MARKET else order.price
            if price is not None and price.is_nan():
                price = None
            txid = await self._gateway.place_order(
                trading_pair=wire_pair,
                order_type=order.order_type.name,  # the gateway maps order types by name
                side=order.trade_type.name.lower(),
                amount=order.amount,
                price=price,
                userref=self._userrefs.get(cid),
            )
        except asyncio.CancelledError:
            logger.warning(
                "create of %s cancelled mid-flight; outcome unknown, order stays tracked",
                order.client_order_id,
            )
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._mark_unknown(order, exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._fail(order, exc)
            return
        finally:
            self._submitting.discard(cid)
        self._on_placed(order, txid)

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
        order.current_state = UNKNOWN_STATE  # type: ignore[assignment]

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
        found = await self._gateway.get_open_orders(userref=userref)
        if not found:
            found = await self._gateway.get_closed_orders(userref=userref)
        # The lookups awaited: AddOrder may have completed or the order been finished meanwhile.
        if order.exchange_order_id is not None or cid not in self.in_flight_orders:
            return
        if found:
            self._on_placed(order, next(iter(found)))
        elif cid not in self._submitting:  # a live AddOrder request may still land
            self._fail(order, LostOrderError(f"order {cid} (userref {userref}) not on Kraken"))

    async def _cancel_order(self, order: InFlightOrder) -> None:
        txid = order.exchange_order_id
        if txid is None:
            return
        try:
            cancelled = await self._gateway.cancel_order(
                trading_pair=self._wire_pair(order.trading_pair), txid=txid
            )
            if cancelled:
                await self._sync_before_cancel(order, txid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("cancel failed for %s", order.client_order_id, exc_info=True)
            return
        finally:
            self._cancelling.discard(order.client_order_id)
        if cancelled:
            self._cancelled(order)

    async def _sync_before_cancel(self, order: InFlightOrder, txid: str) -> None:
        """Book fills the cancel ack raced with, so they precede the 106.

        Skipped while a status poll is already fetching this order's trades: that poll
        books them (as late fills).
        """
        if order.client_order_id in self._syncing_fills:
            return
        info = (await self._gateway.query_orders([txid])).get(txid)
        if info is None:
            return
        if await self._sync_fills(order, info) == _FILLS_TRIGGERED:
            self._synthesize_missing_fill(order, info)

    def _on_placed(self, order: InFlightOrder, txid: str) -> None:
        cid = order.client_order_id
        if order.exchange_order_id is not None:
            return  # already adopted by the userref resolver
        now = time.time()
        self._txid_to_cid[txid] = cid
        self._set_state(order, OrderState.OPEN, now, txid)
        self._emit(created_event(order, txid, now))
        for trade_id, detail in self._pending_trades.pop_for(txid, time.monotonic()):
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
        self._bus.publish(MARKET_EVENT_IDS[type(event).__name__], event)  # type: ignore[arg-type]

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
        self._userref_to_cid.pop(self._userrefs.pop(cid, 0), None)
        self._set_state(order, state, time.time())
        self._finished[cid] = order
        while len(self._finished) > _MAX_FINISHED_ORDERS:
            self._forget(self._finished.pop(next(iter(self._finished))))
        return True

    def _forget(self, order: InFlightOrder) -> None:
        if order.exchange_order_id is not None:
            self._txid_to_cid.pop(order.exchange_order_id, None)

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
            if order is None:
                self._pending_trades.add(trade_id, detail, time.monotonic())
            else:
                self._apply_ws_trade(order, trade_id, detail)

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
        if order.client_order_id not in self.in_flight_orders:
            return
        outcome = await self._sync_fills(order, info)
        if outcome != _FILLS_ERROR and order.client_order_id in self.in_flight_orders:
            self._apply_status(order, info, triggered=outcome == _FILLS_TRIGGERED)

    async def _poll_batch(self, orders: list[InFlightOrder]) -> None:
        txids = [o.exchange_order_id for o in orders if o.exchange_order_id]
        try:
            infos = await self._gateway.query_orders(txids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("batched status poll failed", exc_info=True)
            return
        for order in orders:
            info = infos.get(order.exchange_order_id or "")
            if info is None:
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
        if state is OrderState.FILLED and (
            Decimal(info.vol_exec) >= order.amount or order.executed_amount_base >= order.amount
        ):
            self._complete(order)
        elif state in {OrderState.FILLED, OrderState.CANCELED}:  # closed under-filled = cancelled
            self._cancelled(order)
        elif state is OrderState.OPEN:
            partial = order.executed_amount_base > 0
            self._set_state(
                order,
                OrderState.PARTIALLY_FILLED if partial else OrderState.OPEN,
                time.time(),
            )

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
        self._books[trading_pair] = book.model_copy(update={"trading_pair": trading_pair})

    def get_order_book(self, trading_pair: str) -> OrderBookSnapshot | None:
        """Last fetched snapshot, ``None`` before the first :meth:`update_order_book`."""
        return self._books.get(trading_pair)

    # ------------------------------------------------------------------
    # Trading rules and balances
    # ------------------------------------------------------------------

    async def update_trading_rules(self) -> None:
        """Load AssetPairs: trading rules and the hummingbot pair -> wire pair map."""
        self.trading_rules, self._wire_pairs = build_pair_tables(
            await self._gateway.get_asset_pairs()
        )

    def quantize_order_amount(self, trading_pair: str, amount: Decimal) -> Decimal:
        """Round down to the lot size; 0 if NaN, below the minimum order size or pair unknown."""
        return quantize_amount(self.trading_rules.get(trading_pair), amount)

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

    def get_balance(self, asset: str) -> Decimal:
        return self._balances.get(asset, Decimal(0))

    def get_available_balance(self, asset: str) -> Decimal:
        return self._available.get(asset, Decimal(0))


__all__ = [
    "KRAKEN_ORDER_STATE_MAP",
    "KrakenConnectorCore",
]
