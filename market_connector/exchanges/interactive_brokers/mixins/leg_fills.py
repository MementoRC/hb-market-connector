"""LegFillsMixin: ``SupportsLegFills`` for Interactive Brokers from ``execDetails`` (#249).

Source: the transport's ``FillEvent`` stream (``execDetailsEvent``, already deduplicated by
execId, including ``.NN`` corrections, and replayed by ``get_fills`` / reconnect reconcile).
"""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from market_connector.hb_compat.logging import get_logger
from market_connector.protocols import LegFill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from decimal import Decimal

    from market_connector.exchanges.interactive_brokers.transport import (
        FillEvent,
        SubscriptionHandle,
    )

logger = get_logger(__name__)

_MAX_TRACKED_LEGS = 4096  # last-emitted cumulative per link tag; oldest evicted first


class _Subscriber:
    """One subscriber; an async one drains ``queue`` in order via a single task."""

    def __init__(self, callback: Callable[[LegFill], Awaitable[None] | None]) -> None:
        self.callback = callback
        self.is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)  # noqa: B004 - callable object, async __call__
        )
        # key -> (fill, awaitable). For an async callback the key is the client_order_id and a
        # newer fill replaces the pending one (cumulative: the newest supersedes), so the
        # backlog is bounded by the number of tags. For a sync callback that returned an
        # awaitable (already created, so not coalescable) the key is a unique counter; the
        # drain awaits it in order.
        self.queue: OrderedDict[Any, tuple[LegFill, Awaitable[None] | None]] = OrderedDict()
        self.seq = 0
        self.task: asyncio.Task[None] | None = None
        self.closed = False


class _LegFillHub:
    """Subscribers and per-key running cumulative of one gateway."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.subscribers: list[_Subscriber] = []
        self.emitted: OrderedDict[str, Decimal] = OrderedDict()
        self.bus_handle: SubscriptionHandle | None = None

    def attach(self) -> None:
        if self.bus_handle is None:
            self.bus_handle = self.gateway._transport.subscribe_fills(self.on_fill)

    def detach(self) -> None:
        if self.bus_handle is not None:
            self.bus_handle.close()
            self.bus_handle = None

    def on_fill(self, event: FillEvent) -> None:
        try:
            tag = self.gateway._transport.leg_tag_for(event)
            if tag is None:
                return
            cumulative = event.cum_qty
            previous = self.emitted.get(tag)
            if cumulative <= 0 or (previous is not None and cumulative <= previous):
                return
            self.emitted[tag] = cumulative
            self.emitted.move_to_end(tag)
            while len(self.emitted) > _MAX_TRACKED_LEGS:
                self.emitted.popitem(last=False)
            self._emit(
                LegFill(
                    client_order_id=tag,
                    cumulative_filled=cumulative,
                    last_price=event.price,
                    timestamp=event.time.timestamp(),
                    fill_id=event.exec_id,
                )
            )
        except Exception:
            logger.exception("leg fill conversion failed for exec %r", event.exec_id)

    def _emit(self, fill: LegFill) -> None:
        for sub in list(self.subscribers):
            if sub.is_async:
                self._enqueue(sub, fill, None)
            else:
                try:
                    result = sub.callback(fill)
                    if inspect.isawaitable(result):
                        self._enqueue(sub, fill, result)
                except Exception:
                    logger.exception("leg fill callback failed for %r", fill.client_order_id)

    def _enqueue(self, sub: _Subscriber, fill: LegFill, awaitable: Awaitable[None] | None) -> None:
        if awaitable is None:
            sub.queue[fill.client_order_id] = (fill, None)  # replaces a pending one in place
        else:
            sub.seq += 1
            sub.queue[sub.seq] = (fill, awaitable)
        if sub.task is None:
            sub.task = asyncio.ensure_future(self._drain(sub))

    async def _drain(self, sub: _Subscriber) -> None:
        try:
            while sub.queue and not sub.closed:
                fill, pending = sub.queue.popitem(last=False)[1]
                try:
                    await (pending if pending is not None else sub.callback(fill))  # type: ignore[misc]
                except Exception:
                    logger.exception("leg fill callback failed for %r", fill.client_order_id)
        finally:
            sub.task = None

    def unsubscribe(self, sub: _Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        sub.closed = True
        for _, pending in sub.queue.values():
            if inspect.iscoroutine(pending):
                pending.close()  # never awaited: avoid a "never awaited" warning
        sub.queue.clear()
        if not self.subscribers:
            self.detach()


def _hub(owner: Any) -> _LegFillHub:
    return owner.__dict__.setdefault("_leg_fill_hub", _LegFillHub(owner))  # type: ignore[no-any-return]


class LegFillsMixin:
    def subscribe_leg_fills(
        self: Any, callback: Callable[[LegFill], Awaitable[None] | None]
    ) -> Callable[[], None]:
        """Push a ``LegFill`` (running cumulative volume) per tagged leg, from ``execDetails``.

        ``cumulative_filled`` is the execution's ``cumQty``, ``last_price`` its price,
        ``fill_id`` its ``execId`` and ``timestamp`` the execution time in unix seconds.
        A value is emitted only when it is > 0 and strictly greater than the last one
        emitted for the same ``client_order_id``, so corrections, ``get_fills`` replays and
        reconnect reconciliation never emit twice.

        ``client_order_id`` is the link tag (``orderRef``), not the numeric orderId. It is
        resolved from, in order: ``Execution.orderRef``; the tag recorded at placement for
        the execution's orderId; the tag of the in-process order whose permId matches
        (replays arrive with orderId 0). An untagged order never emits. Attribution of legs
        placed by a previous process (restart) rests on ``Execution.orderRef`` surviving
        onto executions, which is unverified (#312/#319). The last-emitted state is
        in-process and capped (4096 tags, oldest evicted).

        Subscribe before placing legs: fills ingested before the first subscriber attaches
        are not replayed (see #323).

        A sync callback runs inline on the IB event loop. An async callback is driven by
        one drain task per subscriber, in order; under backlog a newer fill of a
        ``client_order_id`` replaces its pending one (the cumulative supersedes it, so the
        backlog is bounded by the number of tags, but intermediate ``fill_id`` values may be
        skipped). A callback error is logged, never
        raised, and other subscribers are still served. Returns an idempotent unsubscribe.
        """
        hub = _hub(self)
        sub = _Subscriber(callback)
        hub.subscribers.append(sub)
        hub.attach()
        return lambda: hub.unsubscribe(sub)
