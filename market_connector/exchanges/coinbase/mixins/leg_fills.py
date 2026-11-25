"""LegFillsMixin: ``SupportsLegFills`` for Coinbase via REST polling (#249).

Coinbase has no authenticated WebSocket user channel (see #310), so the gateway polls
``order_status`` for the legs it placed. Latency of a ``LegFill`` is therefore about
``CoinbaseConfig.leg_fill_poll_interval`` until #310 lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.exchanges.coinbase.schemas.enums import CoinbaseOrderStatus
from market_connector.exchanges.coinbase.schemas.rest import Order
from market_connector.hb_compat.logging import get_logger
from market_connector.leg_fill_hub import LegFillLedger, ReplaySubscriber, schedule_replay
from market_connector.protocols import LegFill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

_CODEC = CoinbaseClientOrderIdLinkCodec()
_MAX_QUEUED = 1024  # per async subscriber backlog before superseded fills are coalesced
_TERMINAL = frozenset(
    {
        CoinbaseOrderStatus.FILLED,
        CoinbaseOrderStatus.CANCELLED,
        CoinbaseOrderStatus.EXPIRED,
        CoinbaseOrderStatus.FAILED,
    }
)


@dataclass
class _Leg:
    """A gateway-placed leg; carries its own exchange id (independent of the id cache)."""

    exchange_id: str
    cumulative: Decimal = Decimal(0)


class _Subscriber(ReplaySubscriber):
    """One subscriber; an async one drains ``queue`` in order via a single task."""

    def __init__(self, callback: Callable[[LegFill], Awaitable[None] | None]) -> None:
        super().__init__()
        self.callback = callback
        self.is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)  # noqa: B004 - callable object, async __call__
        )
        self.queue: deque[LegFill] = deque()
        self.task: asyncio.Task[None] | None = None


class _LegFillHub:
    """Subscribers, tracked legs and the poll task of one gateway."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.subscribers: list[_Subscriber] = []
        self.legs: dict[str, _Leg] = {}  # client_order_id -> non-terminal leg
        self.ledger = LegFillLedger(gateway._config.leg_fill_max_tracked)
        self.poll_task: asyncio.Task[None] | None = None
        self.orphans: set[asyncio.Task[None]] = set()  # drain tasks of unsubscribed subscribers
        self.stopped = False  # set by shutdown; cleared by resume

    # -- tracking and polling ------------------------------------------------

    def track(self, client_order_id: str, exchange_id: str) -> None:
        existing = self.legs.get(client_order_id)
        if existing is not None and existing.exchange_id == exchange_id:
            # Same order re-registered: keep its cumulative so no fill is re-emitted.
            self.legs.pop(client_order_id)
            self.legs[client_order_id] = existing
        else:
            self.legs.pop(client_order_id, None)
            self.legs[client_order_id] = _Leg(exchange_id)
        cap = self.gateway._config.leg_fill_max_tracked
        while len(self.legs) > cap:
            dropped = next(iter(self.legs))
            self.legs.pop(dropped)
            logger.warning("leg fill tracking cap reached; no longer polling %r", dropped)
        self.ensure_polling()

    def ensure_polling(self) -> None:
        if not self.stopped and self.subscribers and self.legs and self.poll_task is None:
            self.poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        try:
            while self.subscribers and self.legs:
                for cid, leg in list(self.legs.items()):
                    await self._poll_leg(cid, leg)
                await asyncio.sleep(self.gateway._config.leg_fill_poll_interval)
        finally:
            if self.poll_task is asyncio.current_task():
                self.poll_task = None

    async def _poll_leg(self, cid: str, leg: _Leg) -> None:
        try:
            response = await self.gateway._rest.request(
                "order_status", params={"order_id": leg.exchange_id}
            )
            order = Order.model_validate(response.raw["order"])
            cumulative = Decimal(order.filled_size)
            if cumulative > leg.cumulative and self.legs.get(cid) is leg:
                # Convert the price first: a malformed value must not advance the
                # cumulative, or the fill would be deduped away on the next valid poll.
                price = Decimal(order.average_filled_price)
                leg.cumulative = cumulative
                self._emit(
                    LegFill(
                        client_order_id=cid,
                        cumulative_filled=cumulative,
                        last_price=price,
                        timestamp=time.time(),
                        fill_id=None,
                    )
                )
            if order.status in _TERMINAL and self.legs.get(cid) is leg:
                del self.legs[cid]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("leg fill poll of %r failed: %r", cid, exc)

    # -- delivery ------------------------------------------------------------

    def _emit(self, fill: LegFill) -> None:
        self.ledger.put(fill)  # kept after the leg leaves ``legs``, for late subscribers
        for sub in list(self.subscribers):
            if sub.admit(fill):
                self.deliver(sub, fill)

    def deliver(self, sub: _Subscriber, fill: LegFill) -> None:
        if sub.is_async:
            sub.queue.append(fill)
            self._bound_queue(sub)
            if sub.task is None:
                sub.task = asyncio.ensure_future(self._drain(sub))
        else:
            self._call_sync(sub, fill)

    @staticmethod
    def _bound_queue(sub: _Subscriber) -> None:
        """Cap a stalled subscriber's backlog by coalescing superseded fills.

        ``cumulative_filled`` is running, so a queued fill is superseded by a later one
        for the same client order id; only such superseded entries are dropped, oldest
        first. The newest fill of every leg is always kept, which bounds the queue by
        ``_MAX_QUEUED`` plus the number of tracked legs.
        """
        while len(sub.queue) > _MAX_QUEUED:
            seen: set[str] = set()
            victim = None
            for idx in range(len(sub.queue) - 1, -1, -1):
                cid = sub.queue[idx].client_order_id
                if cid in seen:
                    victim = idx
                    break
                seen.add(cid)
            if victim is None:
                return
            del sub.queue[victim]

    def _call_sync(self, sub: _Subscriber, fill: LegFill) -> None:
        try:
            result = sub.callback(fill)
            if inspect.isawaitable(result):
                asyncio.ensure_future(self._await_logged(result, fill))  # noqa: RUF006
        except Exception:
            logger.exception("leg fill callback failed for %r", fill.client_order_id)

    @staticmethod
    async def _await_logged(awaitable: Awaitable[None], fill: LegFill) -> None:
        try:
            await awaitable
        except Exception:
            logger.exception("leg fill callback failed for %r", fill.client_order_id)

    async def _drain(self, sub: _Subscriber) -> None:
        try:
            while sub.queue and not sub.closed:
                fill = sub.queue.popleft()
                try:
                    await sub.callback(fill)  # type: ignore[misc]
                except Exception:
                    logger.exception("leg fill callback failed for %r", fill.client_order_id)
        finally:
            sub.task = None

    # -- lifecycle -----------------------------------------------------------

    def unsubscribe(self, sub: _Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        sub.closed = True
        sub.queue.clear()
        if sub.task is not None:
            # An in-flight callback outlives the subscription; keep it for shutdown.
            task = sub.task
            self.orphans.add(task)
            task.add_done_callback(self.orphans.discard)
        if not self.subscribers and self.poll_task is not None:
            self.poll_task.cancel()
            self.poll_task = None

    def resume(self) -> None:
        """Re-enable polling after a stop (gateway restart)."""
        self.stopped = False
        self.ensure_polling()

    async def shutdown(self) -> None:
        self.stopped = True
        tasks = [
            t
            for t in (self.poll_task, *(s.task for s in self.subscribers), *self.orphans)
            if t is not None
        ]
        self.poll_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _hub(owner: Any) -> _LegFillHub:
    return owner.__dict__.setdefault("_leg_fill_hub", _LegFillHub(owner))  # type: ignore[no-any-return]


def track_leg_placement(owner: Any, client_order_id: str, exchange_id: str) -> None:
    """Track a just-placed order for fill polling when its id is a link-codec id.

    Foreign client order ids are never polled or emitted. The exchange id is stored in
    the tracking record itself, so it does not depend on the bounded id cache.
    """
    if _CODEC.decode(client_order_id) is not None:
        _hub(owner).track(client_order_id, exchange_id)


async def stop_leg_fills(owner: Any) -> None:
    """Cancel and await the poll task and async delivery tasks (gateway ``stop``)."""
    hub = owner.__dict__.get("_leg_fill_hub")
    if hub is not None:
        await hub.shutdown()


def resume_leg_fills(owner: Any) -> None:
    """Resume polling for retained subscribers and legs (gateway ``start``)."""
    hub = owner.__dict__.get("_leg_fill_hub")
    if hub is not None:
        hub.resume()


class LegFillsMixin:
    def subscribe_leg_fills(
        self: Any, callback: Callable[[LegFill], Awaitable[None] | None]
    ) -> Callable[[], None]:
        """Push a ``LegFill`` (running cumulative volume) for legs this gateway placed.

        Legs placed through ``place_order`` with a link-codec ``client_order_id`` are polled
        (``order_status``, every ``CoinbaseConfig.leg_fill_poll_interval`` seconds) while
        there is a subscriber and a non-terminal leg. Dedupe: ``cumulative_filled`` is > 0
        and strictly increasing per ``client_order_id`` (a lower or repeated value emits
        nothing). ``fill_id`` is always ``None`` on Coinbase, and ``last_price`` is the
        order's average fill price, not the price of the latest fill. A leg is dropped
        from polling once FILLED / CANCELLED / EXPIRED / FAILED, after any final increase
        is emitted. Latency is about the poll interval until a WS user channel (#310).

        A new subscriber is replayed the latest known cumulative of every leg seen so far,
        including legs already terminal, delivered after this call returns and best-effort
        within ``CoinbaseConfig.leg_fill_max_tracked`` legs; ``get_leg`` is the catch-up for
        evicted legs.

        A sync callback runs inline on the poll task and receives every fill. An async
        callback is driven by one drain task per subscriber, in order, never concurrently.
        A callback error is logged, never raised; a poll error is logged and retried on
        the next tick. Returns an idempotent unsubscribe function; the last unsubscribe
        stops polling.
        """
        hub = _hub(self)
        sub = _Subscriber(callback)
        hub.subscribers.append(sub)
        schedule_replay(hub.ledger, sub, hub.deliver)
        hub.ensure_polling()
        return lambda: hub.unsubscribe(sub)
