"""LegFillsMixin: ``SupportsLegFills`` for Kraken (#249)."""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.kraken.mixins.leg_lookup import link_tag
from market_connector.hb_compat.logging import get_logger
from market_connector.protocols import LegFill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail

logger = get_logger(__name__)

_MAX_TERMINAL = 4096  # records of legs known to be fully filled (kept for trade-id dedup)
_MAX_ACTIVE = 65536  # hard safety cap on records of legs not known to be terminal
_MAX_EARLY = 1024  # unclaimed own-trades buffered for a placement that has not returned yet
_MAX_LAST = 65536  # last emitted cumulative per order instance


def _bounded_put(table: OrderedDict[Any, Any], key: Any, value: Any, cap: int) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > cap:
        table.popitem(last=False)


@dataclass
class _LegRecord:
    """State of one gateway-placed order instance (one txid)."""

    tag: str
    volume: Decimal
    cumulative: Decimal = Decimal(0)
    seen: set[str] = field(default_factory=set)


class _Subscriber:
    """One subscriber; an async one drains ``pending`` (cid -> newest fill) in order."""

    def __init__(self, callback: Callable[[LegFill], Awaitable[None] | None]) -> None:
        self.callback = callback
        self.is_async = inspect.iscoroutinefunction(callback)
        self.pending: OrderedDict[str, LegFill] = OrderedDict()
        self.task: asyncio.Task[None] | None = None
        self.closed = False


class _LegFillHub:
    """Subscribers plus the per-order-instance bookkeeping behind the stream."""

    def __init__(self) -> None:
        self.subscribers: list[_Subscriber] = []
        self.active: OrderedDict[str, _LegRecord] = OrderedDict()  # txid -> record
        self.terminal: OrderedDict[str, _LegRecord] = OrderedDict()  # fully filled legs
        self.early: OrderedDict[tuple[str, str], OwnTradeDetail] = OrderedDict()
        self.last: OrderedDict[str, Decimal] = OrderedDict()  # instance -> last emitted cumulative
        self.tasks: set[asyncio.Task[None]] = set()

    # -- gateway-placed legs -------------------------------------------------

    def record(self, txid: str, tag: str, volume: Decimal) -> None:
        """Start tracking ``txid`` and replay own-trades that arrived before it was known."""
        if txid in self.active or txid in self.terminal:
            return
        self.active[txid] = _LegRecord(tag, volume)
        while len(self.active) > _MAX_ACTIVE:
            self.active.popitem(last=False)
        for key in [k for k in self.early if k[0] == txid]:
            self.claim(txid, key[1], self.early.pop(key))

    def claim(self, txid: str, trade_id: str, detail: OwnTradeDetail) -> bool:
        """Book a trade of a gateway-placed leg; ``False`` (and buffered) if the txid is unknown."""
        rec = self.active.get(txid)
        table = self.active
        if rec is None:
            rec = self.terminal.get(txid)
            table = self.terminal
        if rec is None:
            _bounded_put(self.early, (txid, trade_id), detail, _MAX_EARLY)
            return False
        table.move_to_end(txid)
        if trade_id in rec.seen:
            return True
        rec.seen.add(trade_id)
        rec.cumulative += Decimal(detail.vol)
        self.emit(
            LegFill(
                client_order_id=rec.tag,
                cumulative_filled=rec.cumulative,
                last_price=Decimal(detail.price),
                timestamp=float(detail.time),
                fill_id=trade_id,
            ),
            txid,
        )
        if table is self.active and rec.cumulative >= rec.volume:
            del self.active[txid]
            _bounded_put(self.terminal, txid, rec, _MAX_TERMINAL)
        return True

    # -- delivery ------------------------------------------------------------

    def emit(self, fill: LegFill, instance: str) -> None:
        """Deliver ``fill`` if its cumulative is > 0 and exceeds the last one of ``instance``."""
        if fill.cumulative_filled <= 0:
            return
        previous = self.last.get(instance)
        if previous is not None and fill.cumulative_filled <= previous:
            return
        _bounded_put(self.last, instance, fill.cumulative_filled, _MAX_LAST)
        for sub in list(self.subscribers):
            if sub.is_async:
                self._enqueue(sub, fill)
            else:
                self._call_sync(sub.callback, fill)

    def _call_sync(
        self, callback: Callable[[LegFill], Awaitable[None] | None], fill: LegFill
    ) -> None:
        try:
            result = callback(fill)
            if not inspect.isawaitable(result):
                return
            task = asyncio.ensure_future(result)
        except Exception:
            logger.exception("leg fill callback failed for %r", fill.client_order_id)
            return
        self._track(task)

    def _enqueue(self, sub: _Subscriber, fill: LegFill) -> None:
        if sub.task is None:  # idle: the fill goes in flight at once, later ones coalesce
            sub.task = asyncio.ensure_future(self._drain(sub, fill))
            self._track(sub.task)
        else:
            sub.pending[fill.client_order_id] = fill

    async def _drain(self, sub: _Subscriber, fill: LegFill | None) -> None:
        try:
            while fill is not None and not sub.closed:
                try:
                    await sub.callback(fill)  # type: ignore[misc]
                except Exception:
                    logger.exception("leg fill callback failed for %r", fill.client_order_id)
                fill = sub.pending.popitem(last=False)[1] if sub.pending else None
        finally:
            sub.task = None

    def _track(self, task: asyncio.Task[None]) -> None:
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("async leg fill callback failed", exc_info=task.exception())

    def unsubscribe(self, sub: _Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        sub.closed = True
        sub.pending.clear()


def _hub(owner: Any) -> _LegFillHub:
    hub = getattr(owner, "_leg_fill_hub", None)
    if hub is None:
        hub = _LegFillHub()
        owner._leg_fill_hub = hub
    return hub


def record_leg_placement(
    owner: Any,
    txids: list[str],
    userref: int | None,
    cl_ord_id: str | None,
    volume: Decimal,
) -> None:
    """Remember ``txid -> (link tag, order volume)`` for a just-placed order.

    Untagged orders are skipped. Own-trades that reached the core before this call (the
    fill can beat the AddOrder reply) are replayed from the hub's small early buffer.
    A lost AddOrder reply yields no txid, so such a leg is recovered with
    ``get_leg(...).filled``.

    A record is released to a bounded dedup table once the leg is fully filled. A leg that
    is cancelled while partially filled is retained until the gateway is discarded (the
    hard cap evicts the least recently active record).
    """
    tag = link_tag(userref, cl_ord_id)
    if tag is None:
        return
    hub = _hub(owner)
    for txid in txids:
        hub.record(txid, tag, volume)


class LegFillsMixin:
    def subscribe_leg_fills(
        self: Any, callback: Callable[[LegFill], Awaitable[None] | None]
    ) -> Callable[[], None]:
        """Push a ``LegFill`` (running cumulative volume) for the fills of a leg.

        A sync callback runs inline in the thread that books the trade (the event loop) and
        receives every fill. An async callback (``async def``) is driven by one drain task per
        subscriber: fills are delivered in order and never concurrently, and under backlog a
        newer fill of a ``client_order_id`` replaces its pending one (newest per cid).
        A callback error is logged, never raised. Returns an idempotent unsubscribe function;
        it discards the subscriber's pending fills (a call already in flight completes).

        ``LegFill.client_order_id`` has two forms:

        * core-placed orders (``KrakenConnectorCore.buy`` / ``sell``): the ``hbot-...``
          client order id that ``buy`` / ``sell`` returned (never ``str(userref)``);
        * gateway-placed legs (``place_order``, ``place_bracket``): the leg's link tag,
          i.e. the ``cl_ord_id`` when given, else ``str(userref)`` for a negative link
          userref (the same string as ``LegSnapshot.client_order_id`` from ``get_leg``).
          It is not the txid ``place_order`` returns. Orders placed without a link tag
          emit nothing; a released bracket exit has no known txid and emits nothing.

        ``cumulative_filled`` is > 0 and strictly increasing per order instance (a new
        order reusing a tag starts afresh); each trade is emitted once. Core-placed orders get
        fills from both WS ``ownTrades`` and REST polling. Gateway-placed legs get fills over
        WS ``ownTrades`` only (the core consumes the stream); after a missed frame recover the
        filled volume with ``get_leg(...).filled``.
        """
        hub = _hub(self)
        sub = _Subscriber(callback)
        hub.subscribers.append(sub)
        return lambda: hub.unsubscribe(sub)

    def _emit_leg_fill(self: Any, fill: LegFill) -> None:
        """Publish a fill built by the core for a core-placed order (its cid is unique)."""
        _hub(self).emit(fill, fill.client_order_id)

    def _claim_own_trade(self: Any, trade_id: str, detail: OwnTradeDetail) -> bool:
        """Take a WS trade of a gateway-placed leg; ``False`` when the txid is not one."""
        return _hub(self).claim(detail.ordertxid, trade_id, detail)
