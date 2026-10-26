"""Unit tests for FlatRateLimitSpec (runtime enforcement is covered by hb-rate-limit)."""

from __future__ import annotations

import pytest

from market_connector.rate_limits.specs import FlatRateLimitSpec, PoolSpec


def _make_spec() -> FlatRateLimitSpec:
    """Single-pool spec."""
    pool = PoolSpec(name="public", capacity=10, refill_rate=100.0)
    return FlatRateLimitSpec(
        pools={"public": pool},
        endpoint_pools={"get_ticker": [("public", 1)]},
    )


def test_flat_rate_limit_spec_is_frozen() -> None:
    spec = _make_spec()
    with pytest.raises((AttributeError, TypeError)):
        spec.pools = {}  # type: ignore[misc]
