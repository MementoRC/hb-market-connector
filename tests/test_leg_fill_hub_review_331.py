"""RED regression tests for PR #331 review: shared replay robustness.

* ``subscribe_leg_fills`` must work from sync code with no running loop, even with a
  non-empty ledger (it worked before the replay was added).
* A delivery that raises inside the scheduled replay must not abort the remaining legs.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from market_connector.exchanges.kraken.mixins.leg_fills import LegFillsMixin, _hub
from market_connector.leg_fill_hub import LegFillLedger, ReplaySubscriber, schedule_replay
from market_connector.protocols import LegFill

_D = Decimal


def _fill(cid: str, cum: str) -> LegFill:
    return LegFill(
        client_order_id=cid,
        cumulative_filled=_D(cum),
        last_price=_D("100"),
        timestamp=1.0,
        fill_id=f"{cid}-{cum}",
    )


class _Owner(LegFillsMixin):
    pass


def test_subscribe_without_running_loop_and_nonempty_ledger_does_not_raise() -> None:
    owner = _Owner()
    _hub(owner).ledger.put(_fill("a", "1"))
    received: list[LegFill] = []

    unsubscribe = owner.subscribe_leg_fills(received.append)  # no event loop is running

    assert callable(unsubscribe)
    live = _fill("a", "2")
    _hub(owner).emit(live, "inst-1")
    assert received[-1:] == [live]

    unsubscribe()
    _hub(owner).emit(_fill("a", "3"), "inst-1")
    assert received[-1:] == [live]
    assert _hub(owner).subscribers == []


async def test_failing_deliver_does_not_abort_the_rest_of_the_replay() -> None:
    ledger = LegFillLedger(8)
    ledger.put(_fill("a", "1"))
    ledger.put(_fill("b", "2"))
    delivered: list[str] = []

    def deliver(_sub: Any, fill: LegFill) -> None:
        if fill.client_order_id == "a":
            raise RuntimeError("callback failure")
        delivered.append(fill.client_order_id)

    schedule_replay(ledger, ReplaySubscriber(), deliver)
    for _ in range(4):
        await asyncio.sleep(0)

    assert delivered == ["b"]
