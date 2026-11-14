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
    - The wire pair is the ``altname`` from ``AssetPairs`` (hummingbot pair derived from
      ``wsname``); the symbol mapper's ``to_exchange_pair`` is deliberately not used.
    - Each Completed / Cancelled / Failure event is emitted once per order; fills are
      deduplicated by trade id so REST polling and the WS ``ownTrades`` feed can overlap.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from data_type_primitives.common import OrderType, PriceType, TradeType
from data_type_primitives.in_flight_order import InFlightOrder, OrderState, OrderUpdate

from market_connector.exchanges.kraken.hb_compat.kraken_core_support import (
    NAN,
    TradingRule,
    build_pair_tables,
    build_trade_update,
    quantize_amount,
)
from market_connector.exchanges.kraken.schemas.enums import KrakenOrderState
from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER
from market_connector.hb_compat.logging import get_logger
from market_connector.hb_compat.order_events import (
    MARKET_EVENT_IDS,
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from data_type_primitives.in_flight_order import TradeUpdate

    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.exchanges.kraken.schemas.rest import OrderInfo
    from market_connector.exchanges.kraken.schemas.ws import OwnTradesEvent
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

_MAX_FINISHED_ORDERS = 1000


class KrakenConnectorCore:
    """Order, market-data and balance core for Kraken (see module docstring).

    Args:
        gateway: A started :class:`KrakenGateway`.
        event_bus: Bus the order events are published on.
        status_poll_interval: Seconds between status polls while :meth:`start` is active.
    """

    def __init__(
        self,
        gateway: KrakenGateway,
        *,
        event_bus: EventBus,
        status_poll_interval: float = 10.0,
    ) -> None:
        self._gateway = gateway
        self._bus = event_bus
        self._poll_interval = status_poll_interval
        self._poll_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.in_flight_orders: dict[str, InFlightOrder] = {}
        self._finished: dict[str, InFlightOrder] = {}
        self._txid_to_cid: dict[str, str] = {}
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
        """Cancel and await the polling task (idempotent)."""
        task, self._poll_task = self._poll_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_once()
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        for client_order_id in list(self.in_flight_orders):
            try:
                await self.update_order_status(client_order_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("status poll failed for %s", client_order_id, exc_info=True)

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
        """Cancel in the background; returns ``client_order_id``. Unknown ids are a no-op."""
        loop = asyncio.get_running_loop()
        order = self.in_flight_orders.get(client_order_id)
        if order is None:
            logger.warning("cancel: unknown or finished order %s", client_order_id)
        else:
            self._track(loop.create_task(self._cancel_order(order)))
        return client_order_id

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
        client_order_id = f"hbot-{trade_type.name.lower()}-{uuid.uuid4().hex}"
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

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background order task failed", exc_info=exc)

    async def _submit_order(self, order: InFlightOrder) -> None:
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
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(order, exc)
            return
        self._on_placed(order, txid)

    async def _cancel_order(self, order: InFlightOrder) -> None:
        try:
            txid = await order.get_exchange_order_id()
            if txid is None:
                return
            cancelled = await self._gateway.cancel_order(
                trading_pair=self._wire_pair(order.trading_pair), txid=txid
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("cancel failed for %s", order.client_order_id, exc_info=True)
            return
        if cancelled:
            self._cancelled(order)

    def _on_placed(self, order: InFlightOrder, txid: str) -> None:
        now = time.time()
        self._txid_to_cid[txid] = order.client_order_id
        self._set_state(order, OrderState.OPEN, now, txid)
        created_cls = (
            BuyOrderCreatedEvent if order.trade_type is TradeType.BUY else (SellOrderCreatedEvent)
        )
        self._emit(
            created_cls(
                timestamp=now,
                type=order.order_type,
                trading_pair=order.trading_pair,
                amount=order.amount,
                price=NAN if order.price is None else order.price,
                order_id=order.client_order_id,
                creation_timestamp=order.creation_timestamp,
                exchange_order_id=txid,
                leverage=order.leverage,
                position=order.position.value,
            )
        )

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
        if self.in_flight_orders.pop(order.client_order_id, None) is None:
            return False
        self._set_state(order, state, time.time())
        self._finished[order.client_order_id] = order
        while len(self._finished) > _MAX_FINISHED_ORDERS:
            del self._finished[next(iter(self._finished))]
        return True

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
        if not self._finish(order, OrderState.FILLED):
            return
        completed_cls = (
            BuyOrderCompletedEvent if order.trade_type is TradeType.BUY else SellOrderCompletedEvent
        )
        self._emit(
            completed_cls(
                timestamp=time.time(),
                order_id=order.client_order_id,
                base_asset=order.base_asset,
                quote_asset=order.quote_asset,
                base_asset_amount=order.executed_amount_base,
                quote_asset_amount=order.executed_amount_quote,
                order_type=order.order_type,
                exchange_order_id=order.exchange_order_id,
            )
        )

    def _record_fill(self, order: InFlightOrder, update: TradeUpdate) -> None:
        """Apply a fill once (deduplicated by trade id) and publish ``OrderFilledEvent``."""
        if not order.update_with_trade_update(update):
            return
        self._emit(
            OrderFilledEvent(
                timestamp=update.fill_timestamp,
                order_id=order.client_order_id,
                trading_pair=order.trading_pair,
                trade_type=order.trade_type,
                order_type=order.order_type,
                price=update.fill_price,
                amount=update.fill_base_amount,
                trade_fee=update.fee,
                exchange_trade_id=update.trade_id,
                exchange_order_id=update.exchange_order_id,
                leverage=order.leverage,
                position=order.position.value,
            )
        )

    # ------------------------------------------------------------------
    # Fills: WS ownTrades and REST status polling
    # ------------------------------------------------------------------

    def on_own_trades(self, event: OwnTradesEvent) -> None:
        """Sync WS handler: apply each trade to its in-flight order; ignore strangers."""
        for trade_id, detail in event.trades.items():
            cid = self._txid_to_cid.get(detail.ordertxid)
            order = self.in_flight_orders.get(cid) if cid else None
            if order is None:
                continue
            self._record_fill(
                order,
                build_trade_update(
                    order,
                    trade_id=trade_id,
                    price=Decimal(detail.price),
                    amount=Decimal(detail.vol),
                    cost=Decimal(detail.cost),
                    fee=Decimal(detail.fee),
                    timestamp=float(detail.time),
                ),
            )
            if order.is_filled:
                self._complete(order)

    async def update_order_status(self, client_order_id: str) -> None:
        """REST QueryOrders then QueryTrades; the order status drives completion."""
        order = self.in_flight_orders.get(client_order_id)
        txid = None if order is None else order.exchange_order_id
        if order is None or txid is None:
            return
        info = (await self._gateway.query_orders([txid])).get(txid)
        if info is None or client_order_id not in self.in_flight_orders:
            return
        await self._sync_fills(order, info)
        if client_order_id in self.in_flight_orders:
            self._apply_status(order, info)

    async def _sync_fills(self, order: InFlightOrder, info: OrderInfo) -> None:
        trade_ids = [t for t in info.trades if t not in order.order_fills]
        if not trade_ids:
            return
        try:
            trades = await self._gateway.query_trades(trade_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Triggered orders report trade ids QueryTrades rejects; status still completes.
            logger.debug("query_trades failed for %s: %s", order.client_order_id, exc)
            return
        for trade_id, trade in trades.items():
            self._record_fill(
                order,
                build_trade_update(
                    order,
                    trade_id=trade_id,
                    price=Decimal(trade.price),
                    amount=Decimal(trade.vol),
                    cost=Decimal(trade.cost),
                    fee=Decimal(trade.fee),
                    timestamp=float(trade.time),
                ),
            )

    def _apply_status(self, order: InFlightOrder, info: OrderInfo) -> None:
        try:
            state = KRAKEN_ORDER_STATE_MAP[KrakenOrderState(info.status)]
        except ValueError:
            logger.warning("unknown Kraken order status %r", info.status)
            return
        if state is OrderState.FILLED:
            self._synthesize_missing_fill(order, info)
            self._complete(order)
        elif state is OrderState.CANCELED:
            self._cancelled(order)
        elif state is OrderState.OPEN:
            partial = order.executed_amount_base > 0
            self._set_state(
                order,
                OrderState.PARTIALLY_FILLED if partial else OrderState.OPEN,
                time.time(),
            )

    def _synthesize_missing_fill(self, order: InFlightOrder, info: OrderInfo) -> None:
        """Book the executed volume QueryTrades could not supply (e.g. triggered orders)."""
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
        """Round down to the lot size; 0 if below the minimum order size or pair unknown."""
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
