"""Meta-tests for the RateLimitConformance suite, delegating to rate_limit.testing.

Positive case: a limiter whose real limit matches the declared limit conforms.
Negative case: a limiter whose real capacity differs from the declared limit fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from rate_limit import RateLimit, RateLimiter

from market_connector.testing.contract import RateLimitConformance

if TYPE_CHECKING:
    from rate_limit import Clock, Sleep


def test_rate_limit_conformance_passes_for_conforming_limiter() -> None:
    def make(clock: Clock, sleep: Sleep) -> Any:
        return RateLimiter([RateLimit("a", 3, 1.0)], clock=clock, sleep=sleep).bind("a")

    RateLimitConformance(make, limit=3, interval=1.0).run()


def test_rate_limit_conformance_fails_when_real_capacity_differs() -> None:
    def make(clock: Clock, sleep: Sleep) -> Any:
        return RateLimiter([RateLimit("a", 5, 1.0)], clock=clock, sleep=sleep).bind("a")

    with pytest.raises(AssertionError):
        RateLimitConformance(make, limit=3, interval=1.0).run()
