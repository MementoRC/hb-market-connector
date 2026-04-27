"""Flat rate-limit implementation — one pool set per limiter."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from market_connector.rate_limits.pool import PoolSpec  # noqa: TCH001

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RateLimit(Protocol):
    """Async rate-limit gate checked before each exchange request."""

    async def acquire(self, endpoint_name: str, weight: int = 1) -> None:
        """Block until tokens are available, then consume them."""
        ...


# ---------------------------------------------------------------------------
# Internal per-pool bucket (continuous per-second refill)
# ---------------------------------------------------------------------------


class _RateBucket:
    """Continuous token-bucket with per-second refill.

    Unlike the transport.TokenBucket (window-based), this one refills
    continuously so that partial refills are observable and tests remain
    deterministic with asyncio.sleep.
    """

    def __init__(self, spec: PoolSpec) -> None:
        self._capacity: float = float(spec.capacity)
        self._refill_rate: float = spec.refill_rate  # tokens / second
        self._tokens: float = float(spec.capacity)
        self._last: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last = now

    @property
    def tokens(self) -> float:
        """Current token count after refill (for inspection in tests)."""
        self._refill()
        return self._tokens

    def time_to_tokens(self, needed: float) -> float:
        """Seconds to wait until ``needed`` tokens are available."""
        self._refill()
        deficit = needed - self._tokens
        if deficit <= 0:
            return 0.0
        return deficit / self._refill_rate

    def deduct(self, amount: float) -> None:
        """Deduct tokens without waiting (caller must ensure availability)."""
        self._refill()
        self._tokens -= amount

    def refund(self, amount: float) -> None:
        """Return tokens (used for rollback on partial failure)."""
        self._tokens = min(self._capacity, self._tokens + amount)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlatRateLimitSpec:
    """Declarative rate-limit spec for flat (non-tiered) exchanges.

    Args:
        pools: Pool name → PoolSpec.
        endpoint_pools: Endpoint name → list of (pool_name, weight) charges.
    """

    pools: dict[str, PoolSpec]
    endpoint_pools: dict[str, list[tuple[str, int]]]


# ---------------------------------------------------------------------------
# Runtime limiter
# ---------------------------------------------------------------------------


class FlatRateLimit:
    """Runtime rate limiter built from a :class:`FlatRateLimitSpec`.

    Multi-pool acquires are atomic: tokens are never partially deducted.
    A single ``asyncio.Lock`` serialises all acquire calls so that the
    check-and-deduct cycle is not interleaved.
    """

    def __init__(self, spec: FlatRateLimitSpec) -> None:
        self._spec = spec
        self._buckets: dict[str, _RateBucket] = {
            name: _RateBucket(pool_spec) for name, pool_spec in spec.pools.items()
        }
        self._lock = asyncio.Lock()

    async def acquire(self, endpoint_name: str, weight: int = 1) -> None:
        """Block until all pools can satisfy the endpoint's weighted cost."""
        charges = self._spec.endpoint_pools[endpoint_name]  # KeyError on unknown

        while True:
            async with self._lock:
                # Compute max wait needed across all pools
                max_wait = max(
                    self._buckets[pool_name].time_to_tokens(w * weight) for pool_name, w in charges
                )
                if max_wait <= 0:
                    # All pools satisfied — deduct atomically
                    for pool_name, w in charges:
                        self._buckets[pool_name].deduct(w * weight)
                    return

            # Sleep outside the lock so other coroutines can proceed
            await asyncio.sleep(max_wait)
