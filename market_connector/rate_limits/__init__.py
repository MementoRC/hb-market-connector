"""Rate limiting — declarative specs translated into hb-rate-limit limiters."""

from market_connector.exceptions import RateLimitExceeded
from market_connector.rate_limits.limiter import (
    ensure_keyed_limit,
    limiter_from_flat_spec,
    limiter_from_tiered_spec,
    pool_to_rate_limit,
)
from market_connector.rate_limits.specs import (
    FlatRateLimitSpec,
    PoolSpec,
    TieredRateLimitSpec,
    TierProfile,
)

__all__ = [
    "limiter_from_flat_spec",
    "limiter_from_tiered_spec",
    "pool_to_rate_limit",
    "ensure_keyed_limit",
    "PoolSpec",
    "FlatRateLimitSpec",
    "TierProfile",
    "TieredRateLimitSpec",
    "RateLimitExceeded",
]
