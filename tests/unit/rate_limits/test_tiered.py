"""Unit tests for TierProfile and TieredRateLimitSpec (enforcement lives in hb-rate-limit)."""

from __future__ import annotations

import pytest

from market_connector.rate_limits.specs import PoolSpec, TieredRateLimitSpec, TierProfile


def _make_tiered_spec() -> TieredRateLimitSpec:
    """Three-tier spec mimicking a Kraken-style exchange."""
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


def test_tier_profile_is_frozen() -> None:
    tp = TierProfile(name="T", pools={})
    with pytest.raises((AttributeError, TypeError)):
        tp.name = "X"  # type: ignore[misc]


def test_tiered_spec_is_frozen() -> None:
    spec = _make_tiered_spec()
    with pytest.raises((AttributeError, TypeError)):
        spec.public_pools = {}  # type: ignore[misc]
