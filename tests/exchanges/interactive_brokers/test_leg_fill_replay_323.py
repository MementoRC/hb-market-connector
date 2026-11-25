"""RED acceptance tests for #323 (Interactive Brokers): late-subscriber ``LegFill`` replay.

Contract: a subscriber that calls ``subscribe_leg_fills`` AFTER fills were ingested receives
the latest known ``LegFill`` of every tagged leg, including fills ingested or reconciled
before the first subscribe ever happened (the hub tracks from transport start).

* Replay is scheduled, not inline: the callback is not invoked before ``subscribe_leg_fills``
  returns; it runs after the loop is yielded to (sync and async callbacks alike).
* Each new subscriber gets the replay once; subscribers already attached get no duplicate.
* A live fill after the replay is delivered normally and stays strictly increasing; a live
  fill whose cumulative equals the replayed value is not re-emitted to that subscriber.

Runs without ib_async: fills are fed through ``MockIb.execDetailsEvent`` and
``IbGatewayTransport._ingest_fill`` with the in-repo fake fills.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb, make_fill

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
TAG_A = "mcl-5-0"
TAG_B = "mcl-5-1"
_D = Decimal


class Sink:
    def __init__(self) -> None:
        self.fills: list[LegFill] = []

    def on_fill(self, fill: LegFill) -> None:
        self.fills.append(fill)

    async def on_fill_async(self, fill: LegFill) -> None:
        self.fills.append(fill)

    @property
    def cumulative(self) -> list[Decimal]:
        return [f.cumulative_filled for f in self.fills]


async def _started() -> tuple[IbGatewayGateway, IbGatewayTransport, MockIb]:
    """A connected gateway over MockIb; no leg-fill subscriber exists yet."""
    ib = MockIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec())
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
    await gateway.start()
    return gateway, transport, ib


def _exec(ib: MockIb, exec_id: str, cum: float, tag: str = TAG_A) -> None:
    """Live execution of ``tag`` reaching running total ``cum`` (shares = cum for the test)."""
    ib.execDetailsEvent(
        None, make_fill(exec_id, order_id=11, cum_qty=cum, shares=cum, order_ref=tag)
    )


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def _callback(sink: Sink, kind: str) -> Any:
    return sink.on_fill if kind == "sync" else sink.on_fill_async


class TestReplayToLateSubscriber:
    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_fill_ingested_before_first_subscribe_is_replayed(self, kind: str) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        sink = Sink()

        gateway.subscribe_leg_fills(_callback(sink, kind))
        await _settle()

        assert [(f.client_order_id, f.cumulative_filled, f.fill_id) for f in sink.fills] == [
            (TAG_A, _D(4), "e1.01")
        ]

    async def test_reconciled_fill_ingested_before_first_subscribe_is_replayed(self) -> None:
        gateway, transport, _ = await _started()
        fill = make_fill("e1.01", order_id=0, cum_qty=6, shares=6, order_ref=TAG_A)
        assert transport._ingest_fill(fill, reconciled=True) is True
        sink = Sink()

        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        assert sink.cumulative == [_D(6)]

    async def test_only_the_latest_cumulative_of_each_leg_is_replayed(self) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "a1.01", 4, TAG_A)
        _exec(ib, "b1.01", 7, TAG_B)
        _exec(ib, "a2.01", 10, TAG_A)
        sink = Sink()

        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        assert sorted((f.client_order_id, f.cumulative_filled) for f in sink.fills) == [
            (TAG_A, _D(10)),
            (TAG_B, _D(7)),
        ]

    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_replay_is_scheduled_not_inline(self, kind: str) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        sink = Sink()

        gateway.subscribe_leg_fills(_callback(sink, kind))

        assert sink.fills == []
        await _settle()
        assert sink.cumulative == [_D(4)]


class TestIdempotenceAndIsolation:
    async def test_attached_subscriber_gets_no_duplicate_when_another_joins(self) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        first, second = Sink(), Sink()
        gateway.subscribe_leg_fills(first.on_fill)
        await _settle()

        gateway.subscribe_leg_fills(second.on_fill)
        await _settle()

        assert first.cumulative == [_D(4)]
        assert second.cumulative == [_D(4)]

    async def test_each_new_subscriber_gets_the_replay_once(self) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        sink = Sink()

        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()
        await _settle()

        assert sink.cumulative == [_D(4)]

    async def test_live_fill_after_replay_is_delivered_and_strictly_increasing(self) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        _exec(ib, "e2.01", 10)
        await _settle()

        assert sink.cumulative == [_D(4), _D(10)]

    async def test_live_fill_equal_to_replayed_value_is_not_re_emitted(self) -> None:
        gateway, _, ib = await _started()
        _exec(ib, "e1.01", 4)
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        _exec(ib, "e2.01", 4)
        await _settle()

        assert [f.fill_id for f in sink.fills] == ["e1.01"]
