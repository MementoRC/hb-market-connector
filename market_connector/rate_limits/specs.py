"""Declarative rate-limit specs, translated into an hb-rate-limit RateLimiter
by rate_limits.limiter."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PoolSpec:
    """Declarative descriptor for a single token-bucket pool.

    Args:
        name: Human-readable pool identifier.
        capacity: Maximum tokens in the bucket (must be > 0).
        refill_rate: Tokens added per second (must be > 0).
    """

    name: str
    capacity: int
    refill_rate: float

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {self.capacity!r}")
        if self.refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {self.refill_rate!r}")


@dataclass(frozen=True)
class FlatRateLimitSpec:
    """Declarative rate-limit spec for flat (non-tiered) exchanges.

    Args:
        pools: Pool name → PoolSpec.
        endpoint_pools: Endpoint name → list of (pool_name, weight) charges.
    """

    pools: dict[str, PoolSpec]
    endpoint_pools: dict[str, list[tuple[str, int]]]


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
        keyed_endpoint_pools: Endpoint name → (template_pool, weight) charges for
            endpoints limited per key (e.g. per currency pair). Each key gets its
            own copy of the template pool, registered lazily at runtime.
    """

    public_pools: dict[str, PoolSpec]
    tiers: dict[str, TierProfile]
    endpoint_pools: dict[str, list[tuple[str, int]]]
    keyed_endpoint_pools: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
