"""Regression tests for PR #321 review feedback on #249 leg fills."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from .test_leg_fills_249 import TAG_A, TAG_B, _exec, _gateway, _place

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

pytest.importorskip("ib_async")


class TestSlowAsyncSubscriberCoalesces:
    async def test_backlog_keeps_newest_cumulative_per_tag_and_is_bounded(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        release = asyncio.Event()

        async def callback(fill: LegFill) -> None:
            await release.wait()
            got.append(fill)

        gateway.subscribe_leg_fills(callback)
        for i in range(1, 6):
            _exec(ib, f"e{i}", oid, i, tag=TAG_A)
        await asyncio.sleep(0.01)
        (sub,) = gateway.__dict__["_leg_fill_hub"].subscribers
        assert len(sub.queue) <= 1  # one tag -> at most one pending fill
        release.set()
        await asyncio.sleep(0.05)
        assert [f.cumulative_filled for f in got][-1] == 5
        assert len(got) <= 2  # first in flight + the coalesced newest


class TestAwaitableReturningSyncCallback:
    async def test_returned_awaitables_complete_in_fill_order(self) -> None:
        gateway, ib = await _gateway()
        oid_a = await _place(gateway, ib, TAG_A)
        oid_b = await _place(gateway, ib, TAG_B)
        done: list[str] = []

        async def _work(fill: LegFill, delay: float) -> None:
            await asyncio.sleep(delay)
            done.append(fill.client_order_id)

        def callback(fill: LegFill) -> Any:
            # A plain function returning a coroutine: not detected as a coroutine function.
            return _work(fill, 0.05 if fill.client_order_id == TAG_A else 0.0)

        gateway.subscribe_leg_fills(callback)
        _exec(ib, "e1", oid_a, 5, tag=TAG_A)
        _exec(ib, "e2", oid_b, 5, tag=TAG_B)
        await asyncio.sleep(0.2)
        assert done == [TAG_A, TAG_B]
