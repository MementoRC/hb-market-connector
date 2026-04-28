"""Unit tests for FlatRateLimitSpec and FlatRateLimit."""

from __future__ import annotations

import asyncio

import pytest

from market_connector.rate_limits.flat import FlatRateLimit, FlatRateLimitSpec, RateLimit
from market_connector.rate_limits.pool import PoolSpec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(capacity: int = 10, refill_rate: float = 100.0) -> FlatRateLimitSpec:
    """Single-pool spec used for most tests."""
    pool = PoolSpec(name="public", capacity=capacity, refill_rate=refill_rate)
    return FlatRateLimitSpec(
        pools={"public": pool},
        endpoint_pools={"get_ticker": [("public", 1)]},
    )


def _make_dual_spec(a_cap: int = 10, b_cap: int = 10) -> FlatRateLimitSpec:
    """Two-pool spec for atomicity tests."""
    pool_a = PoolSpec(name="pool_a", capacity=a_cap, refill_rate=100.0)
    pool_b = PoolSpec(name="pool_b", capacity=b_cap, refill_rate=100.0)
    return FlatRateLimitSpec(
        pools={"pool_a": pool_a, "pool_b": pool_b},
        endpoint_pools={
            "dual_endpoint": [("pool_a", 1), ("pool_b", 2)],
        },
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_rate_limit_protocol_satisfied() -> None:
    limiter = FlatRateLimit(_make_spec())
    assert isinstance(limiter, RateLimit)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_succeeds_when_tokens_available() -> None:
    limiter = FlatRateLimit(_make_spec(capacity=5))
    # Should not raise or block
    await asyncio.wait_for(limiter.acquire("get_ticker"), timeout=0.5)


@pytest.mark.asyncio
async def test_acquire_unknown_endpoint_raises_key_error() -> None:
    limiter = FlatRateLimit(_make_spec())
    with pytest.raises(KeyError):
        await limiter.acquire("no_such_endpoint")


# ---------------------------------------------------------------------------
# Blocking behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_blocks_when_bucket_empty() -> None:
    """After exhausting capacity=2, third acquire must block beyond a tight timeout."""
    limiter = FlatRateLimit(_make_spec(capacity=2, refill_rate=0.5))  # slow refill
    await limiter.acquire("get_ticker")
    await limiter.acquire("get_ticker")
    # Bucket now empty; should block
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("get_ticker"), timeout=0.05)


@pytest.mark.asyncio
async def test_acquire_unblocks_after_refill() -> None:
    """After exhausting bucket, acquire eventually completes once refilled."""
    # capacity=1, refill_rate=20 → refills 1 token in ~0.05s
    limiter = FlatRateLimit(_make_spec(capacity=1, refill_rate=20.0))
    await limiter.acquire("get_ticker")  # exhaust
    # Should complete within 0.3s (generous for CI)
    await asyncio.wait_for(limiter.acquire("get_ticker"), timeout=0.3)


# ---------------------------------------------------------------------------
# Multi-pool deduction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_pool_both_deducted_on_acquire() -> None:
    """A dual-endpoint acquire must deduct from both pools."""
    # Use very slow refill_rate so token count is stable during the test
    pool_a = PoolSpec(name="pool_a", capacity=10, refill_rate=0.01)
    pool_b = PoolSpec(name="pool_b", capacity=10, refill_rate=0.01)
    spec = FlatRateLimitSpec(
        pools={"pool_a": pool_a, "pool_b": pool_b},
        endpoint_pools={"dual_endpoint": [("pool_a", 1), ("pool_b", 2)]},
    )
    limiter = FlatRateLimit(spec)
    before_a = limiter._buckets["pool_a"].tokens
    before_b = limiter._buckets["pool_b"].tokens
    await limiter.acquire("dual_endpoint", weight=1)
    # pool_a cost=1*1=1, pool_b cost=2*1=2
    # Use generous abs tolerance for any tiny timing drift
    assert limiter._buckets["pool_a"].tokens == pytest.approx(before_a - 1, abs=0.05)
    assert limiter._buckets["pool_b"].tokens == pytest.approx(before_b - 2, abs=0.05)


# ---------------------------------------------------------------------------
# Atomicity: pool_b insufficient → pool_a must NOT be deducted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_no_partial_deduction_when_pool_b_insufficient() -> None:
    """pool_a has plenty; pool_b is empty.  acquire must NOT deduct pool_a."""
    # pool_b capacity=1, but endpoint costs (pool_b, 2) so it can never satisfy
    # immediately.  Use very slow refill so it blocks beyond our timeout.
    pool_a = PoolSpec(name="pool_a", capacity=10, refill_rate=100.0)
    pool_b = PoolSpec(name="pool_b", capacity=1, refill_rate=0.1)  # ~10s to refill 1
    spec = FlatRateLimitSpec(
        pools={"pool_a": pool_a, "pool_b": pool_b},
        endpoint_pools={"costly": [("pool_a", 1), ("pool_b", 2)]},
    )
    limiter = FlatRateLimit(spec)

    tokens_a_before = limiter._buckets["pool_a"].tokens

    # This should block because pool_b can never reach 2 tokens within 0.05s
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("costly"), timeout=0.05)

    # pool_a MUST NOT have been deducted
    assert limiter._buckets["pool_a"].tokens == pytest.approx(tokens_a_before, abs=0.01)


# ---------------------------------------------------------------------------
# Spec is frozen
# ---------------------------------------------------------------------------


def test_flat_rate_limit_spec_is_frozen() -> None:
    spec = _make_spec()
    with pytest.raises((AttributeError, TypeError)):
        spec.pools = {}  # type: ignore[misc]
