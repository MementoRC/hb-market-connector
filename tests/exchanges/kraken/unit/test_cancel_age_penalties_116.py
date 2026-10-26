"""Acceptance tests: Kraken cancel age penalties on the per-pair matching pool (#116).

Clock injection contract: the orders mixin reads time from ``self._order_clock``,
a zero-arg monotonic callable returning seconds (defaults to ``time.monotonic``).
Tests set it on the ``OrdersMixin`` instance.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from rate_limit.testing import ManualClock

from market_connector.exchanges.kraken.age_penalties import (
    amend_penalty,
    cancel_penalty,
    edit_penalty,
)
from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.primitives import OrderType, TradeType
from market_connector.rate_limits.limiter import limiter_from_tiered_spec
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rate_limit import RateLimiter

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "rest"
PAIR = "XBTUSD"
OTHER_PAIR = "ETHUSD"
CAPACITY = 60  # STARTER matching pool
UNKNOWN_TXID = "OXXXXX-XXXXX-XXXXXX"
UNKNOWN_COST = 8


def _load(name: str) -> dict:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


class _Orders(OrdersMixin):
    def __init__(self, rest: RestConnectorBase, clock: ManualClock) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._order_clock = clock

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def order_clock() -> ManualClock:
    """Order-age clock; kept separate so ageing does not refill the limiter's pools."""
    return ManualClock()


@pytest.fixture
def limiter() -> RateLimiter:
    frozen = ManualClock()
    return limiter_from_tiered_spec(
        KRAKEN_RATE_LIMIT_SPEC, "STARTER", clock=frozen, sleep=frozen.sleep
    )


@pytest.fixture
def rest(limiter: RateLimiter) -> RestConnectorBase:
    return RestConnectorBase(
        "https://api.kraken.com", endpoints=ENDPOINT_REGISTRY, rate_limiter=limiter
    )


@pytest.fixture
def send(rest: RestConnectorBase) -> Iterator[AsyncMock]:
    def _respond(*, url: str, **_: object) -> httpx.Response:
        name = "cancel_order.json" if url.endswith("CancelOrder") else "add_order.json"
        return httpx.Response(200, json=_load(name))

    with patch.object(rest, "_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=_respond)
        yield mock_client.request


@pytest.fixture
def orders(rest: RestConnectorBase, order_clock: ManualClock, send: AsyncMock) -> _Orders:
    return _Orders(rest, order_clock)


def _remaining(limiter: RateLimiter, pair: str) -> int:
    """Units the pair's matching pool still admits (consumes them)."""
    pool = f"matching:{pair}"
    count = 0
    while limiter.try_acquire(pool, {pool: 1}) == 0:
        count += 1
    return count


async def _place(orders: _Orders, pair: str = PAIR) -> str:
    return await orders.place_order(pair, OrderType.MARKET, TradeType.BUY, Decimal("1"))


def _cancel_calls(send: AsyncMock) -> int:
    return sum(1 for c in send.call_args_list if c.kwargs["url"].endswith("CancelOrder"))


class TestPenaltyTables:
    """AC1: pinned penalty tables (total matching units, fixed + age penalty)."""

    @pytest.mark.parametrize(
        ("age", "cost"),
        [
            (0, 8),
            (4.999, 8),
            (5.0, 6),
            (9.999, 6),
            (10.0, 5),
            (14.999, 5),
            (15.0, 4),
            (44.999, 4),
            (45.0, 2),
            (89.999, 2),
            (90.0, 1),
            (299.999, 1),
            (300.0, 0),
            (10_000, 0),
        ],
    )
    def test_cancel_penalty(self, age: float, cost: int) -> None:
        assert cancel_penalty(age) == cost

    @pytest.mark.parametrize(
        ("age", "cost"),
        [(0, 4), (4.999, 4), (5.0, 3), (9.999, 3), (10.0, 2), (14.999, 2), (15.0, 1), (500, 1)],
    )
    def test_amend_penalty(self, age: float, cost: int) -> None:
        assert amend_penalty(age) == cost

    @pytest.mark.parametrize(
        ("age", "cost"),
        [
            (0, 7),
            (4.999, 7),
            (5.0, 6),
            (10.0, 5),
            (15.0, 3),
            (44.999, 3),
            (45.0, 2),
            (89.999, 2),
            (90.0, 1),
            (500, 1),
        ],
    )
    def test_edit_penalty(self, age: float, cost: int) -> None:
        assert edit_penalty(age) == cost


class TestCancelCharging:
    async def test_cancel_at_age_2s_consumes_8(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        """AC2 + AC3: age is measured from the clock reading at place_order."""
        txid = await _place(orders)  # consumes 1 (add_order)
        order_clock.advance(2.0)

        await orders.cancel_order(PAIR, txid)

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 8

    async def test_age_is_taken_from_placement_time(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        """AC2: place at t=1000 (not 0); cancel 100s later costs 1, not the unknown 8."""
        order_clock.advance(1000.0)
        txid = await _place(orders)
        order_clock.advance(100.0)

        await orders.cancel_order(PAIR, txid)

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 1

    async def test_cancel_of_order_older_than_300s_is_free_but_still_sent(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)

        assert await orders.cancel_order(PAIR, txid) is True

        assert _cancel_calls(send) == 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1

    async def test_cancel_at_exactly_300s_is_free(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(300.0)

        await orders.cancel_order(PAIR, txid)

        assert _cancel_calls(send) == 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1

    async def test_unknown_txid_charges_worst_case_8(
        self, orders: _Orders, limiter: RateLimiter, send: AsyncMock
    ) -> None:
        """AC4: order not placed by this process."""
        await orders.cancel_order(PAIR, UNKNOWN_TXID)

        assert _cancel_calls(send) == 1
        assert _remaining(limiter, PAIR) == CAPACITY - UNKNOWN_COST

    async def test_txid_forgotten_after_successful_cancel(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        """AC5: the second cancel of the same txid is treated as unknown (8)."""
        txid = await _place(orders)
        order_clock.advance(400.0)
        await orders.cancel_order(PAIR, txid)  # free

        await orders.cancel_order(PAIR, txid)

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - UNKNOWN_COST

    async def test_other_pairs_pools_unaffected(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        """AC6."""
        txid = await _place(orders)
        order_clock.advance(2.0)
        await orders.cancel_order(PAIR, txid)
        await orders.cancel_order(OTHER_PAIR, UNKNOWN_TXID)

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 8
        assert _remaining(limiter, OTHER_PAIR) == CAPACITY - UNKNOWN_COST

    async def test_cancel_charges_do_not_leak_to_untouched_pair(
        self, orders: _Orders, limiter: RateLimiter, send: AsyncMock
    ) -> None:
        """AC6: a pair that was never traded keeps its full capacity."""
        await orders.cancel_order(PAIR, UNKNOWN_TXID)
        await _place(orders, OTHER_PAIR)

        assert _remaining(limiter, OTHER_PAIR) == CAPACITY - 1
