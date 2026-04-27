"""Unit tests for PoolSpec validation."""

import pytest

from market_connector.rate_limits.pool import PoolSpec


def test_pool_spec_valid() -> None:
    spec = PoolSpec(name="public", capacity=10, refill_rate=1.0)
    assert spec.name == "public"
    assert spec.capacity == 10
    assert spec.refill_rate == 1.0


def test_pool_spec_capacity_zero_raises() -> None:
    with pytest.raises(ValueError, match="capacity"):
        PoolSpec(name="p", capacity=0, refill_rate=1.0)


def test_pool_spec_capacity_negative_raises() -> None:
    with pytest.raises(ValueError, match="capacity"):
        PoolSpec(name="p", capacity=-5, refill_rate=1.0)


def test_pool_spec_refill_rate_zero_raises() -> None:
    with pytest.raises(ValueError, match="refill_rate"):
        PoolSpec(name="p", capacity=10, refill_rate=0)


def test_pool_spec_refill_rate_negative_raises() -> None:
    with pytest.raises(ValueError, match="refill_rate"):
        PoolSpec(name="p", capacity=10, refill_rate=-1.0)


def test_pool_spec_is_frozen() -> None:
    spec = PoolSpec(name="p", capacity=5, refill_rate=2.0)
    with pytest.raises((AttributeError, TypeError)):
        spec.capacity = 99  # type: ignore[misc]
