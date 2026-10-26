"""Acceptance tests: declared zero-cost cancels (#131) and bounded batch-cancel limits (#130).

Observable behaviour only. The registry is inspected via ``RateLimiter.limits()``
(the read-only snapshot of registered limit ids), the same limiter object the
connector charges.

- #131: a cancel of an order aged >= 300s charges nothing on ``matching:{pair}`` yet is
  still sent through a limit declared by ``KRAKEN_RATE_LIMIT_SPEC`` (no lazily created
  ``POST:/path`` default limit), for single cancels and for all-zero batches.
- #130: ``cancel_orders_batch`` must not register one limit per distinct SET of pairs.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from rate_limit.testing import ManualClock

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
_ADD_SEQ = itertools.count()
_SPEC = KRAKEN_RATE_LIMIT_SPEC
_BASE_IDS = (
    set(_SPEC.public_pools)
    | {name for tier in _SPEC.tiers.values() for name in tier.pools}
    | set(_SPEC.endpoint_pools)
    | set(_SPEC.keyed_endpoint_pools)
)
_TEMPLATES = {pool for charges in _SPEC.keyed_endpoint_pools.values() for pool, _ in charges}


def _load(name: str) -> dict:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


def _respond(*, url: str, **kwargs: object) -> httpx.Response:
    name = url.rsplit("/", 1)[-1]
    if name == "CancelOrderBatch":
        body = kwargs.get("json") or json.loads(kwargs["content"])  # type: ignore[arg-type]
        return httpx.Response(200, json={"error": [], "result": {"count": len(body["orders"])}})
    payload = _load({"CancelOrder": "cancel_order.json"}.get(name, "add_order.json"))
    if name == "AddOrder":
        payload["result"]["txid"] = [f"OPR{next(_ADD_SEQ):03d}-AAAAA-AAAAAA"]
    return httpx.Response(200, json=payload)


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
    return ManualClock()


@pytest.fixture
def limiter() -> RateLimiter:
    frozen = ManualClock()
    return limiter_from_tiered_spec(_SPEC, "STARTER", clock=frozen, sleep=frozen.sleep)


@pytest.fixture
def rest(limiter: RateLimiter) -> RestConnectorBase:
    return RestConnectorBase(
        "https://api.kraken.com", endpoints=ENDPOINT_REGISTRY, rate_limiter=limiter
    )


@pytest.fixture
def send(rest: RestConnectorBase) -> Iterator[AsyncMock]:
    with patch.object(rest, "_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=_respond)
        yield mock_client.request


@pytest.fixture
def orders(rest: RestConnectorBase, order_clock: ManualClock, send: AsyncMock) -> _Orders:
    return _Orders(rest, order_clock)


def _remaining(limiter: RateLimiter, pair: str) -> int:
    pool = f"matching:{pair}"
    count = 0
    while limiter.try_acquire(pool, {pool: 1}) == 0:
        count += 1
    return count


def _ids(limiter: RateLimiter) -> set[str]:
    return set(limiter.limits())


def _undeclared(limiter: RateLimiter) -> set[str]:
    """Registered ids that are neither spec-declared nor a declared ``name:{pair}`` copy."""
    declared_prefixes = {f"{name}:" for name in _TEMPLATES | set(_SPEC.keyed_endpoint_pools)}
    return {
        lid
        for lid in _ids(limiter)
        if lid not in _BASE_IDS and not lid.startswith(tuple(declared_prefixes))
    }


async def _place(orders: _Orders, pair: str = PAIR) -> str:
    return await orders.place_order(pair, OrderType.MARKET, TradeType.BUY, Decimal("1"))


def _sent(send: AsyncMock, suffix: str) -> int:
    return sum(1 for c in send.call_args_list if c.kwargs["url"].endswith(suffix))


class TestZeroCostCancelDeclared131:
    async def test_old_cancel_consumes_nothing_from_matching_pool(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)

        assert await orders.cancel_order(PAIR, txid) is True

        assert _sent(send, "CancelOrder") == 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1

    async def test_old_cancel_creates_no_lazy_default_limit(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)
        await orders.cancel_order(PAIR, txid)

        assert "POST:/0/private/CancelOrder" not in _ids(limiter)
        assert _undeclared(limiter) == set()

    async def test_all_zero_batch_creates_no_lazy_default_limit(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)

        await orders.cancel_orders_batch([(PAIR, txid)])

        assert _sent(send, "CancelOrderBatch") == 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1
        assert "POST:/0/private/CancelOrderBatch" not in _ids(limiter)
        assert _undeclared(limiter) == set()

    async def test_all_zero_multi_pair_batch_creates_no_lazy_default_limit(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        btc = await _place(orders, PAIR)
        eth = await _place(orders, OTHER_PAIR)
        order_clock.advance(400.0)

        await orders.cancel_orders_batch([(PAIR, btc), (OTHER_PAIR, eth)])

        assert _undeclared(limiter) == set()
        assert not any("+" in lid for lid in _ids(limiter))


class TestBatchLimitsBounded130:
    async def test_distinct_pair_subsets_do_not_grow_registry(
        self, orders: _Orders, limiter: RateLimiter
    ) -> None:
        pairs = [f"PAIR{i}USD" for i in range(6)]
        await orders.cancel_orders_batch([(p, UNKNOWN_TXID) for p in pairs])  # warm every pair
        baseline = len(_ids(limiter))

        subsets = [
            combo for size in (2, 3) for combo in itertools.combinations(pairs, size)
        ]  # 15 + 20 = 35 distinct sets
        assert len(subsets) >= 20
        for combo in subsets:
            await orders.cancel_orders_batch([(p, UNKNOWN_TXID) for p in combo])

        assert len(_ids(limiter)) == baseline
        assert not any("+" in lid for lid in _ids(limiter))

    async def test_batch_charges_unchanged_while_bounded(
        self, orders: _Orders, limiter: RateLimiter
    ) -> None:
        await orders.cancel_orders_batch([(PAIR, UNKNOWN_TXID), (OTHER_PAIR, UNKNOWN_TXID)])

        assert _remaining(limiter, PAIR) == CAPACITY - 8
        assert _remaining(limiter, OTHER_PAIR) == CAPACITY - 8
        assert not any("+" in lid for lid in _ids(limiter))
