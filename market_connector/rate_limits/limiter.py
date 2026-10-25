"""Translate declarative pool specs into an hb-rate-limit ``RateLimiter``.

Mapping:

* Each :class:`PoolSpec` becomes a token-bucket ``RateLimit`` named after the pool,
  with ``time_interval = capacity / refill_rate`` (see :func:`pool_to_rate_limit`).
* Each spec endpoint becomes a ``RateLimit`` named after the endpoint whose
  ``linked_limits`` carry its ``(pool, weight)`` charges. Its own bucket is sized to
  the largest linked pool so it never binds before the pools do.

``RestConnectorBase`` reserves by endpoint name, so one request atomically charges
every pool the endpoint draws from, and pools are shared across endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rate_limit import LinkedLimitWeightPair, RateLimit, RateLimiter

if TYPE_CHECKING:
    from rate_limit import Clock, Sleep

    from market_connector.rate_limits.flat import FlatRateLimitSpec
    from market_connector.rate_limits.pool import PoolSpec
    from market_connector.rate_limits.tiered import TieredRateLimitSpec


def pool_to_rate_limit(pool: PoolSpec) -> RateLimit:
    """Map a pool to a token-bucket limit of the same capacity and refill rate."""
    return RateLimit(
        pool.name, pool.capacity, pool.capacity / pool.refill_rate, kind="token_bucket"
    )


def _build_limiter(
    pools: dict[str, PoolSpec],
    endpoint_pools: dict[str, list[tuple[str, int]]],
    clock: Clock | None,
    sleep: Sleep | None,
) -> RateLimiter:
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    limiter = RateLimiter(**kwargs)  # type: ignore[arg-type]
    limiter.add_limits(pool_to_rate_limit(pool) for pool in pools.values())
    # Token bucket, not a window: a window stays full after pools refill and would bind first.
    limiter.add_limits(
        RateLimit(
            name,
            max(pools[pool].capacity for pool, _ in charges),
            1.0,
            linked_limits=[LinkedLimitWeightPair(pool, weight) for pool, weight in charges],
            kind="token_bucket",
        )
        for name, charges in endpoint_pools.items()
    )
    return limiter


def limiter_from_flat_spec(
    spec: FlatRateLimitSpec, *, clock: Clock | None = None, sleep: Sleep | None = None
) -> RateLimiter:
    """Build a ``RateLimiter`` enforcing a flat spec's pools and endpoint charges."""
    return _build_limiter(spec.pools, spec.endpoint_pools, clock, sleep)


def limiter_from_tiered_spec(
    spec: TieredRateLimitSpec,
    tier: str,
    *,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
) -> RateLimiter:
    """Build a ``RateLimiter`` from the public pools plus ``tier``'s private pools.

    Raises:
        KeyError: ``tier`` is not in ``spec.tiers``.
    """
    if tier not in spec.tiers:
        raise KeyError(f"Unknown tier {tier!r}; valid: {list(spec.tiers)}")
    pools = {**spec.public_pools, **spec.tiers[tier].pools}
    return _build_limiter(pools, spec.endpoint_pools, clock, sleep)
