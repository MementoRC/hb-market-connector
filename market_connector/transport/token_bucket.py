"""Async token bucket rate limiter."""

from __future__ import annotations

import time

from market_connector.exceptions import RateLimitError


class TokenBucket:
    """Simple token bucket that refills fully after each window elapses."""

    def __init__(self, rate: int, window: float) -> None:
        self._rate = rate
        self._window = window
        self._tokens = rate
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed >= self._window:
            self._tokens = self._rate
            self._last_refill = now

    async def acquire(self, weight: int = 1) -> None:
        """Consume tokens or raise RateLimitError if exhausted."""
        self._refill()
        if self._tokens < weight:
            raise RateLimitError(
                f"rate limit exhausted: {self._tokens} tokens remaining, "
                f"need {weight}, refills in {self._window}s"
            )
        self._tokens -= weight
