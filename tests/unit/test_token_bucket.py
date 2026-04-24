"""Tests for TokenBucket rate limiter."""

import asyncio

import pytest

from market_connector.exceptions import RateLimitError
from market_connector.transport.token_bucket import TokenBucket


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_acquire_within_limit(self) -> None:
        bucket = TokenBucket(rate=10, window=1.0)
        for _ in range(10):
            await bucket.acquire(weight=1)

    @pytest.mark.asyncio
    async def test_acquire_exhausted_raises(self) -> None:
        bucket = TokenBucket(rate=2, window=1.0)
        await bucket.acquire(weight=1)
        await bucket.acquire(weight=1)
        with pytest.raises(RateLimitError, match="rate limit"):
            await bucket.acquire(weight=1)

    @pytest.mark.asyncio
    async def test_acquire_with_weight(self) -> None:
        bucket = TokenBucket(rate=10, window=1.0)
        await bucket.acquire(weight=5)
        await bucket.acquire(weight=5)
        with pytest.raises(RateLimitError):
            await bucket.acquire(weight=1)

    @pytest.mark.asyncio
    async def test_tokens_refill_after_window(self) -> None:
        bucket = TokenBucket(rate=1, window=0.1)
        await bucket.acquire(weight=1)
        with pytest.raises(RateLimitError):
            await bucket.acquire(weight=1)
        await asyncio.sleep(0.2)
        await bucket.acquire(weight=1)  # Should succeed after refill
