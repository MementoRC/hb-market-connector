"""Meta-tests for RateLimitConformance suite (Task 11).

Positive case: permissive FlatRateLimit with large bucket → all acquires complete
              well within the expected duration ceiling.
Negative case: suite provided with a ceiling that is unreachably tight →
              suite.run() raises AssertionError.
"""

from __future__ import annotations

import pytest

from market_connector.rate_limits.flat import FlatRateLimit
from market_connector.testing.contract import RateLimitConformance
from market_connector.testing.spec_fixtures import KNOWN_FLAT_RATE_LIMIT_SPEC


@pytest.mark.asyncio
async def test_rate_limit_conformance_passes_with_permissive_limit() -> None:
    """RateLimitConformance.run() completes without error when the bucket is large.

    The fixture spec has capacity=1000 / refill=1000 per second, so 5 weight-1
    acquires drain ~0.5% of capacity and take effectively 0 seconds wall time.
    A 2-second ceiling gives ≥1.5s headroom.
    """
    limiter = FlatRateLimit(KNOWN_FLAT_RATE_LIMIT_SPEC)
    request_stream = [("test_endpoint", 1)] * 5
    suite = RateLimitConformance(
        rate_limit=limiter,
        request_stream=request_stream,
        expected_max_duration_seconds=2.0,
    )
    await suite.run()


@pytest.mark.asyncio
async def test_rate_limit_conformance_fails_with_negative_ceiling() -> None:
    """RateLimitConformance.run() raises AssertionError when ceiling is negative."""
    limiter = FlatRateLimit(KNOWN_FLAT_RATE_LIMIT_SPEC)
    request_stream = [("test_endpoint", 1)] * 2
    suite = RateLimitConformance(
        rate_limit=limiter,
        request_stream=request_stream,
        expected_max_duration_seconds=-1.0,  # impossible: any elapsed time > -1
    )
    with pytest.raises(AssertionError):
        await suite.run()
