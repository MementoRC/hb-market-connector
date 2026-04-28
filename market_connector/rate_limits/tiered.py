"""Tiered rate-limit implementation — public pools plus per-tier private pools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from market_connector.rate_limits.flat import _RateBucket
from market_connector.rate_limits.pool import PoolSpec  # noqa: TCH001


@dataclass(frozen=True)
class TierProfile:
    """Private pool set for a single tier level.

    Args:
        name: Tier identifier (e.g. ``"STARTER"``).
        pools: Pool name → PoolSpec for this tier's private pools.
    """

    name: str
    pools: dict[str, PoolSpec]


@dataclass(frozen=True)
class TieredRateLimitSpec:
    """Declarative rate-limit spec for tiered exchanges (e.g. Kraken).

    Args:
        public_pools: Shared pools applied regardless of tier.
        tiers: Tier name → :class:`TierProfile`.
        endpoint_pools: Endpoint name → list of (pool_name, weight) charges.
    """

    public_pools: dict[str, PoolSpec]
    tiers: dict[str, TierProfile]
    endpoint_pools: dict[str, list[tuple[str, int]]]


class TieredRateLimit:
    """Runtime rate limiter built from a :class:`TieredRateLimitSpec`.

    Holds buckets for ``public_pools`` AND the active tier's pools.  Multi-pool
    acquires are atomic under a single ``asyncio.Lock``.

    Args:
        spec: The declarative spec.
        active_tier: Key into ``spec.tiers``; raises ``KeyError`` if unknown.
    """

    def __init__(self, spec: TieredRateLimitSpec, active_tier: str) -> None:
        if active_tier not in spec.tiers:
            raise KeyError(f"Unknown tier {active_tier!r}; valid: {list(spec.tiers)}")
        self._spec = spec
        self._active_tier = active_tier

        # Build all buckets: public first, then tier-private
        self._buckets: dict[str, _RateBucket] = {
            name: _RateBucket(pool_spec) for name, pool_spec in spec.public_pools.items()
        }
        for name, pool_spec in spec.tiers[active_tier].pools.items():
            self._buckets[name] = _RateBucket(pool_spec)

        self._lock = asyncio.Lock()

    async def acquire(self, endpoint_name: str, weight: int = 1) -> None:
        """Block until all relevant pools can satisfy the endpoint's cost."""
        charges = self._spec.endpoint_pools[endpoint_name]  # KeyError on unknown

        while True:
            async with self._lock:
                max_wait = max(
                    self._buckets[pool_name].time_to_tokens(w * weight) for pool_name, w in charges
                )
                if max_wait <= 0:
                    for pool_name, w in charges:
                        self._buckets[pool_name].deduct(w * weight)
                    return

            await asyncio.sleep(max_wait)
