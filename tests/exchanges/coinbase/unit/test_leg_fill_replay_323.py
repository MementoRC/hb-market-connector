"""RED acceptance tests for #323 (Coinbase): late-subscriber ``LegFill`` replay.

Contract: a subscriber that calls ``subscribe_leg_fills`` after an earlier subscriber saw
fills receives the latest known ``LegFill`` of every leg, including the final total of legs
that already reached a terminal state (today those are dropped from ``legs``). Polling still
starts only once a subscriber exists; the replay covers what was seen while an earlier
subscriber was attached.

* Replay is scheduled, not inline: the callback is not invoked before ``subscribe_leg_fills``
  returns; it runs after the loop is yielded to (sync and async callbacks alike).
* Each new subscriber gets the replay once; subscribers already attached get no duplicate.
* A live fill after the replay is delivered normally and stays strictly increasing; a poll
  that reports the replayed value again is not re-emitted to that subscriber.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tests.exchanges.coinbase.unit import test_leg_fills_249 as base

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway

gateway = base.gateway
rest = base.rest

_D = Decimal
_A = base._A
Sink = base.Sink


@pytest.fixture
def unsubs() -> Iterator[list[Callable[[], None]]]:
    handles: list[Callable[[], None]] = []
    yield handles
    for unsub in handles:
        unsub()


def _callback(sink: Any, kind: str) -> Any:
    return sink.on_fill if kind == "sync" else sink.on_fill_async


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def _seen_by_first(
    gateway: CoinbaseGateway,  # noqa: F811
    rest: base._FakeRest,  # noqa: F811
    unsubs: list[Callable[[], None]],
    *rows: Any,
    keep_attached: bool = True,
) -> tuple[str, Any]:
    """Place ``_A``, attach a first subscriber and wait until it has seen every row's total."""
    xid = await base._place(gateway, _A)
    base._script(rest, xid, *rows)
    first = Sink()
    unsub = gateway.subscribe_leg_fills(first.on_fill)
    unsubs.append(unsub)
    assert await base._wait_until(lambda: first.cumulative[-1:] == [_D(rows[-1]["filled_size"])])
    await base._quiet()
    if not keep_attached:
        unsub()
    return xid, first


class TestReplayToLateSubscriber:
    @pytest.mark.parametrize("keep_attached", [True, False])
    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_terminal_leg_final_total_is_replayed(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
        kind: str,
        keep_attached: bool,
    ) -> None:
        await _seen_by_first(
            gateway,
            rest,
            unsubs,
            base._row("1.0", "FILLED", "50010.5"),
            keep_attached=keep_attached,
        )
        late = Sink()

        unsubs.append(gateway.subscribe_leg_fills(_callback(late, kind)))
        await _settle()

        assert [(f.client_order_id, f.cumulative_filled, f.last_price) for f in late.fills] == [
            (_A, _D("1.0"), _D("50010.5"))
        ]

    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_active_leg_latest_cumulative_is_replayed(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
        kind: str,
    ) -> None:
        await _seen_by_first(gateway, rest, unsubs, base._row("0.3", price="50001"))
        late = Sink()

        unsubs.append(gateway.subscribe_leg_fills(_callback(late, kind)))
        await _settle()
        await base._quiet()

        assert late.cumulative == [_D("0.3")]

    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_replay_is_scheduled_not_inline(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
        kind: str,
    ) -> None:
        await _seen_by_first(gateway, rest, unsubs, base._row("1.0", "FILLED"))
        late = Sink()

        unsubs.append(gateway.subscribe_leg_fills(_callback(late, kind)))

        assert late.fills == []
        await _settle()
        assert late.cumulative == [_D("1.0")]


class TestIdempotenceAndIsolation:
    async def test_attached_subscriber_gets_no_duplicate_when_another_joins(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
    ) -> None:
        _, first = await _seen_by_first(gateway, rest, unsubs, base._row("1.0", "FILLED"))
        late = Sink()

        unsubs.append(gateway.subscribe_leg_fills(late.on_fill))
        await _settle()
        await base._quiet()

        assert first.cumulative == [_D("1.0")]
        assert late.cumulative == [_D("1.0")]

    async def test_poll_reporting_the_replayed_value_again_is_not_re_emitted(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
    ) -> None:
        await _seen_by_first(gateway, rest, unsubs, base._row("0.3"))
        late = Sink()

        unsubs.append(gateway.subscribe_leg_fills(late.on_fill))
        await base._quiet(10)

        assert late.cumulative == [_D("0.3")]

    async def test_live_fill_after_replay_is_delivered_and_strictly_increasing(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        unsubs: list[Callable[[], None]],
    ) -> None:
        xid, first = await _seen_by_first(gateway, rest, unsubs, base._row("0.3"))
        late = Sink()
        unsubs.append(gateway.subscribe_leg_fills(late.on_fill))
        await _settle()

        base._script(rest, xid, base._row("0.7"))
        assert await base._wait_until(lambda: late.cumulative[-1:] == [_D("0.7")])

        assert late.cumulative == [_D("0.3"), _D("0.7")]
        assert first.cumulative == [_D("0.3"), _D("0.7")]
