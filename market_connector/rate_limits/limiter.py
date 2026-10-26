"""Translate declarative pool specs into an hb-rate-limit ``RateLimiter``.

Mapping:

* Each :class:`PoolSpec` becomes a token-bucket ``RateLimit`` named after the pool,
  with ``time_interval = capacity / refill_rate`` (see :func:`pool_to_rate_limit`).
* Each spec endpoint becomes a ``RateLimit`` named after the endpoint whose
  ``linked_limits`` carry its ``(pool, weight)`` charges. Its own bucket is sized to
  the largest linked pool so it never binds before the pools do.

* Endpoints limited per key (e.g. Kraken matching per currency pair) are not built
  up front; :func:`ensure_keyed_limit` lazily registers a per-key copy of a template
  pool plus a per-key endpoint limit, reserved via ``request(limit_id=...)``.

``RestConnectorBase`` reserves by endpoint name, so one request atomically charges
every pool the endpoint draws from, and pools are shared across endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rate_limit import LinkedLimitWeightPair, RateLimit, RateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rate_limit import Clock, Sleep

    from market_connector.rate_limits.specs import FlatRateLimitSpec, PoolSpec, TieredRateLimitSpec


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


def ensure_keyed_limit(
    limiter: RateLimiter,
    endpoint_name: str,
    key: str,
    charges: list[tuple[str, int]],
) -> str:
    """Register (idempotently) a per-key endpoint limit and return its limit id.

    Each ``(pool, weight)`` charge gets a pool ``"{pool}:{key}"`` copied from the
    template pool ``pool``; the endpoint limit ``"{endpoint_name}:{key}"`` links them.
    Endpoints sharing a key therefore share that key's pools.

    Raises:
        KeyError: a template pool is not registered on ``limiter``.
    """
    limit_id = f"{endpoint_name}:{key}"
    if limiter.has_limit(limit_id):
        return limit_id
    capacity = 1
    for pool, _ in charges:
        template = limiter.get_raw_limit(pool)
        if template is None:
            raise KeyError(f"No template pool {pool!r} registered for keyed limit {limit_id!r}")
        capacity = max(capacity, template.limit)
        keyed_pool = f"{pool}:{key}"
        if not limiter.has_limit(keyed_pool):
            limiter.add_limit(
                RateLimit(keyed_pool, template.limit, template.time_interval, kind=template.kind)
            )
    limiter.add_limit(
        RateLimit(
            limit_id,
            capacity,
            1.0,
            linked_limits=[LinkedLimitWeightPair(f"{pool}:{key}", w) for pool, w in charges],
            kind="token_bucket",
        )
    )
    return limit_id


def ensure_multi_keyed_limit(
    limiter: RateLimiter,
    endpoint_name: str,
    keys: Iterable[str],
    charges: list[tuple[str, int]],
) -> str:
    """Register (idempotently) one endpoint limit linking the pools of several keys.

    For a request that charges several keys at once (a Kraken cancel batch across
    pairs). Each key's pools are the ones :func:`ensure_keyed_limit` creates, so the
    charges land on the same pools as the single-key endpoints. The limit is named
    ``"{endpoint_name}:{key1}+{key2}..."`` over the sorted distinct keys; reserve it
    with ``weights`` naming the keyed pools to charge.
    """
    distinct = sorted(set(keys))
    single_ids = [ensure_keyed_limit(limiter, endpoint_name, key, charges) for key in distinct]
    if len(distinct) == 1:
        return single_ids[0]
    limit_id = f"{endpoint_name}:{'+'.join(distinct)}"
    if not limiter.has_limit(limit_id):
        limiter.add_limit(
            RateLimit(
                limit_id,
                max(limiter.get_raw_limit(pool).limit for pool, _ in charges),  # type: ignore[union-attr]
                1.0,
                linked_limits=[
                    LinkedLimitWeightPair(f"{pool}:{key}", w)
                    for key in distinct
                    for pool, w in charges
                ],
                kind="token_bucket",
            )
        )
    return limit_id


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
