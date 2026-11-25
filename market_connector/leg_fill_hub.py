"""Shared late-subscriber replay of ``LegFill`` (#323).

``LegFillLedger`` remembers the latest ``LegFill`` per leg in a bounded table. A venue hub
records into it as fills are accepted and, when a new subscriber joins, calls
``schedule_replay`` to deliver the latest known cumulative per leg to that subscriber only,
after ``subscribe_leg_fills`` has returned.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from decimal import Decimal

    from market_connector.protocols import LegFill

DEFAULT_MAX_LEG_FILLS = 4096


class LegFillLedger:
    """Latest ``LegFill`` per ``client_order_id``; the oldest leg is evicted when full."""

    def __init__(self, max_entries: int = DEFAULT_MAX_LEG_FILLS) -> None:
        self._max_entries = max_entries
        self._latest: OrderedDict[str, LegFill] = OrderedDict()

    def record(self, fill: LegFill) -> bool:
        """Keep ``fill`` and return True only if its cumulative strictly increases."""
        previous = self._latest.get(fill.client_order_id)
        if previous is not None and fill.cumulative_filled <= previous.cumulative_filled:
            return False
        self.put(fill)
        return True

    def put(self, fill: LegFill) -> None:
        """Keep ``fill`` as the latest of its leg unconditionally (caller already deduped)."""
        self._latest[fill.client_order_id] = fill
        self._latest.move_to_end(fill.client_order_id)
        while len(self._latest) > self._max_entries:
            self._latest.popitem(last=False)

    def latest(self) -> Iterator[LegFill]:
        return iter(list(self._latest.values()))


class ReplaySubscriber:
    """Base of a venue ``_Subscriber``: tracks what its replay promised.

    ``floor`` holds, per leg, the cumulative it was replayed (raised by later live
    deliveries); ``admit`` drops a live fill that is not above it.
    """

    def __init__(self) -> None:
        self.floor: dict[str, Decimal] = {}
        self.closed = False

    def admit(self, fill: LegFill) -> bool:
        floor = self.floor.get(fill.client_order_id)
        if floor is None:
            return True
        if fill.cumulative_filled <= floor:
            return False
        self.floor[fill.client_order_id] = fill.cumulative_filled
        return True


def schedule_replay(
    ledger: LegFillLedger, sub: ReplaySubscriber, deliver: Callable[[Any, LegFill], None]
) -> None:
    """Replay ``ledger.latest()`` to ``sub`` alone, on the next loop iteration.

    The snapshot is taken now and becomes the subscriber's floor, so a live fill at or
    below it is dropped; a leg that advanced live before the replay runs is skipped.
    """
    snapshot = list(ledger.latest())
    if not snapshot:
        return
    for fill in snapshot:
        sub.floor[fill.client_order_id] = fill.cumulative_filled

    def run() -> None:
        for fill in snapshot:
            if not sub.closed and sub.floor.get(fill.client_order_id) == fill.cumulative_filled:
                deliver(sub, fill)

    asyncio.get_running_loop().call_soon(run)
