"""Acceptance tests for the per-call ``weights`` override on REST requests (#116, AC0)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from rate_limit import LinkedLimitWeightPair, RateLimit, RateLimiter
from rate_limit.testing import ManualClock

from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase

BASE = "https://api.example.com"
POOL_CAPACITY = 10


def _ok() -> httpx.Response:
    return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def limiter(clock: ManualClock) -> RateLimiter:
    rl = RateLimiter(clock=clock, sleep=clock.sleep)
    rl.add_limit(RateLimit("pool", POOL_CAPACITY, 1000.0))
    rl.add_limit(
        RateLimit(
            "ep",
            POOL_CAPACITY,
            1.0,
            linked_limits=[LinkedLimitWeightPair("pool", 1)],
            kind="token_bucket",
        )
    )
    return rl


@pytest.fixture
def client(limiter: RateLimiter) -> RestConnectorBase:
    endpoints = {"order": Endpoint(path="/api/order", method="POST", limit=5, window=1.0)}
    return RestConnectorBase(BASE, endpoints=endpoints, rate_limiter=limiter)


def _remaining(limiter: RateLimiter, pool: str) -> int:
    """Count how many unit charges the pool still admits (consumes them)."""
    count = 0
    while limiter.try_acquire(pool, {pool: 1}) == 0:
        count += 1
    return count


async def _send(client: RestConnectorBase, **kwargs: object) -> None:
    with patch.object(client, "_client") as mock_client:
        mock_client.request = AsyncMock(return_value=_ok())
        await client.request("order", limit_id="ep", **kwargs)
    mock_client.request.assert_awaited_once()


class TestWeightsOverride:
    async def test_weights_override_linked_pool_charge(
        self, client: RestConnectorBase, limiter: RateLimiter
    ) -> None:
        await _send(client, weights={"pool": 4})

        assert _remaining(limiter, "pool") == POOL_CAPACITY - 4

    async def test_default_weights_keep_linked_pool_weight(
        self, client: RestConnectorBase, limiter: RateLimiter
    ) -> None:
        await _send(client)

        assert _remaining(limiter, "pool") == POOL_CAPACITY - 1

    async def test_explicit_none_equals_default(
        self, client: RestConnectorBase, limiter: RateLimiter
    ) -> None:
        await _send(client, weights=None)

        assert _remaining(limiter, "pool") == POOL_CAPACITY - 1

    async def test_override_larger_than_remaining_waits_not_raises(
        self, client: RestConnectorBase, clock: ManualClock
    ) -> None:
        await _send(client, weights={"pool": 8})
        # 2 left; a second weight-8 call must wait for refill rather than fail
        before = clock()
        await _send(client, weights={"pool": 8})

        assert clock() > before

    async def test_weights_key_outside_linked_limits_raises_before_send(
        self, client: RestConnectorBase
    ) -> None:
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            with pytest.raises(ValueError, match="weights key"):
                await client.request("order", limit_id="ep", weights={"other": 2})
        mock_client.request.assert_not_called()
