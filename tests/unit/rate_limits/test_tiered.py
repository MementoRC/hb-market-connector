"""Unit tests for TierProfile, TieredRateLimitSpec, and TieredRateLimit."""

from __future__ import annotations

import asyncio

import pytest

from market_connector.rate_limits.pool import PoolSpec
from market_connector.rate_limits.tiered import TieredRateLimit, TieredRateLimitSpec, TierProfile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tiered_spec() -> TieredRateLimitSpec:
    """Three-tier spec mimicking a Kraken-style exchange."""
    # Use very slow refill_rate for public so exhaustion-then-block tests are stable
    public = PoolSpec(name="public", capacity=20, refill_rate=0.01)

    starter_private = PoolSpec(name="private", capacity=5, refill_rate=1.0)
    intermediate_private = PoolSpec(name="private", capacity=10, refill_rate=2.0)
    pro_private = PoolSpec(name="private", capacity=20, refill_rate=4.0)

    return TieredRateLimitSpec(
        public_pools={"public": public},
        tiers={
            "STARTER": TierProfile(name="STARTER", pools={"private": starter_private}),
            "INTERMEDIATE": TierProfile(
                name="INTERMEDIATE", pools={"private": intermediate_private}
            ),
            "PRO": TierProfile(name="PRO", pools={"private": pro_private}),
        },
        endpoint_pools={
            "GetTicker": [("public", 1)],
            "AddOrder": [("public", 1), ("private", 1)],
        },
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_unknown_tier_raises_key_error() -> None:
    spec = _make_tiered_spec()
    with pytest.raises(KeyError, match="GOLD"):
        TieredRateLimit(spec, active_tier="GOLD")


def test_valid_tier_constructs() -> None:
    spec = _make_tiered_spec()
    limiter = TieredRateLimit(spec, active_tier="STARTER")
    assert "public" in limiter._buckets
    assert "private" in limiter._buckets


# ---------------------------------------------------------------------------
# Tier isolation — STARTER has lower private capacity than PRO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starter_tier_blocks_at_starter_capacity() -> None:
    """STARTER private pool (capacity=5) exhausted after 5 acquires of AddOrder."""
    spec = _make_tiered_spec()
    limiter = TieredRateLimit(spec, active_tier="STARTER")

    for _ in range(5):
        await asyncio.wait_for(limiter.acquire("AddOrder"), timeout=0.5)

    # 6th acquire should block (private exhausted, slow refill_rate=1.0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("AddOrder"), timeout=0.05)


@pytest.mark.asyncio
async def test_pro_tier_allows_higher_capacity() -> None:
    """PRO private pool (capacity=20) can serve 20 AddOrder requests."""
    spec = _make_tiered_spec()
    limiter = TieredRateLimit(spec, active_tier="PRO")

    for _ in range(20):
        await asyncio.wait_for(limiter.acquire("AddOrder"), timeout=0.5)

    # 21st blocks
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("AddOrder"), timeout=0.05)


# ---------------------------------------------------------------------------
# Public-only endpoint not affected by private tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_endpoint_uses_only_public_pool() -> None:
    """GetTicker only hits public pool regardless of tier."""
    spec = _make_tiered_spec()
    limiter = TieredRateLimit(spec, active_tier="STARTER")

    for _ in range(20):
        await asyncio.wait_for(limiter.acquire("GetTicker"), timeout=0.5)

    # 21st blocks (public cap=20)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("GetTicker"), timeout=0.05)


# ---------------------------------------------------------------------------
# Atomicity: public ok, private insufficient → no partial deduction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_no_partial_deduction_across_public_and_private() -> None:
    """AddOrder hits both public and private; private empty → public must not deduct."""
    public = PoolSpec(name="public", capacity=10, refill_rate=100.0)
    private = PoolSpec(name="private", capacity=1, refill_rate=0.1)  # slow

    spec = TieredRateLimitSpec(
        public_pools={"public": public},
        tiers={
            "STARTER": TierProfile(
                name="STARTER",
                pools={"private": private},
            ),
        },
        endpoint_pools={"AddOrder": [("public", 1), ("private", 2)]},
    )
    limiter = TieredRateLimit(spec, active_tier="STARTER")

    public_before = limiter._buckets["public"].tokens

    # private can never satisfy weight=2 within 0.05s (capacity=1)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("AddOrder"), timeout=0.05)

    # public pool must remain untouched
    assert limiter._buckets["public"].tokens == pytest.approx(public_before, abs=0.01)


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


def test_tier_profile_is_frozen() -> None:
    tp = TierProfile(name="T", pools={})
    with pytest.raises((AttributeError, TypeError)):
        tp.name = "X"  # type: ignore[misc]


def test_tiered_spec_is_frozen() -> None:
    spec = _make_tiered_spec()
    with pytest.raises((AttributeError, TypeError)):
        spec.public_pools = {}  # type: ignore[misc]
