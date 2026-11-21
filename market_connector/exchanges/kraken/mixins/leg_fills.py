"""LegFillsMixin: ``SupportsLegFills`` for Kraken (#249)."""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.kraken.mixins.leg_lookup import link_tag
from market_connector.hb_compat.logging import get_logger
from market_connector.protocols import LegFill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail

logger = get_logger(__name__)

_MAX_TRACKED = 4096


def _bounded_put(table: OrderedDict[Any, Any], key: Any, value: Any) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > _MAX_TRACKED:
        table.popitem(last=False)


class _LegFillHub:
    """Subscribers plus the per-leg bookkeeping behind the stream."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[LegFill], Awaitable[None] | None]] = []
        self.tags: OrderedDict[str, str] = OrderedDict()  # txid -> link tag
        self.cumulative: OrderedDict[str, Decimal] = OrderedDict()  # txid -> filled
        self.seen: OrderedDict[tuple[str, str], None] = OrderedDict()  # (txid, trade id)
        self.last: OrderedDict[str, Decimal] = OrderedDict()  # cid -> last emitted cumulative
        self.tasks: set[asyncio.Task[None]] = set()

    def emit(self, fill: LegFill) -> None:
        """Deliver ``fill`` unless its cumulative does not exceed the last one for its cid."""
        previous = self.last.get(fill.client_order_id)
        if previous is not None and fill.cumulative_filled <= previous:
            return
        _bounded_put(self.last, fill.client_order_id, fill.cumulative_filled)
        for callback in list(self.callbacks):
            self._deliver(callback, fill)

    def _deliver(
        self, callback: Callable[[LegFill], Awaitable[None] | None], fill: LegFill
    ) -> None:
        try:
            result = callback(fill)
            if not inspect.isawaitable(result):
                return
            task = asyncio.ensure_future(result)
        except Exception:
            logger.exception("leg fill callback failed for %s", fill.client_order_id)
            return
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("async leg fill callback failed", exc_info=task.exception())


def _hub(owner: Any) -> _LegFillHub:
    hub = getattr(owner, "_leg_fill_hub", None)
    if hub is None:
        hub = _LegFillHub()
        owner._leg_fill_hub = hub
    return hub


def record_leg_placement(
    owner: Any, txids: list[str], userref: int | None, cl_ord_id: str | None
) -> None:
    """Remember ``txid -> link tag`` for a just-placed order; untagged orders are skipped."""
    tag = link_tag(userref, cl_ord_id)
    if tag is None:
        return
    hub = _hub(owner)
    for txid in txids:
        _bounded_put(hub.tags, txid, tag)


class LegFillsMixin:
    def subscribe_leg_fills(
        self: Any, callback: Callable[[LegFill], Awaitable[None] | None]
    ) -> Callable[[], None]:
        """Push a ``LegFill`` (running cumulative volume) for every fill of a leg.

        ``callback`` may be sync or async. A sync callback runs inline in the thread that
        books the trade (the event loop); an async one is scheduled as a task on the
        running loop. A callback error is logged, never raised. Returns an idempotent
        unsubscribe function.

        ``LegFill.client_order_id`` has two forms:

        * core-placed orders (``KrakenConnectorCore.buy`` / ``sell``): the ``hbot-...``
          client order id that ``buy`` / ``sell`` returned (never ``str(userref)``);
        * gateway-placed legs (``place_order``, ``place_bracket``): the leg's link tag,
          i.e. the ``cl_ord_id`` when given, else ``str(userref)`` for a negative link
          userref (the same string as ``LegSnapshot.client_order_id`` from ``get_leg``).
          It is not the txid ``place_order`` returns. Orders placed without a link tag
          emit nothing; a released bracket exit has no known txid and emits nothing.

        ``cumulative_filled`` is strictly increasing per ``client_order_id``; each trade
        is emitted once. Core-placed orders get fills from both WS ``ownTrades`` and REST
        polling. Gateway-placed legs get fills over WS ``ownTrades`` only (the core
        consumes the stream); after a missed frame recover the filled volume with
        ``get_leg(...).filled``.
        """
        hub = _hub(self)
        hub.callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in hub.callbacks:
                hub.callbacks.remove(callback)

        return unsubscribe

    def _emit_leg_fill(self: Any, fill: LegFill) -> None:
        """Publish a fill built by the core for a core-placed order."""
        _hub(self).emit(fill)

    def _claim_own_trade(self: Any, trade_id: str, detail: OwnTradeDetail) -> bool:
        """Take a WS trade of a gateway-placed leg; ``False`` when the txid is not one."""
        hub = _hub(self)
        txid = detail.ordertxid
        tag = hub.tags.get(txid)
        if tag is None:
            return False
        key = (txid, trade_id)
        if key in hub.seen:
            return True
        _bounded_put(hub.seen, key, None)
        total = hub.cumulative.get(txid, Decimal(0)) + Decimal(detail.vol)
        _bounded_put(hub.cumulative, txid, total)
        hub.emit(
            LegFill(
                client_order_id=tag,
                cumulative_filled=total,
                last_price=Decimal(detail.price),
                timestamp=float(detail.time),
                fill_id=trade_id,
            )
        )
        return True
