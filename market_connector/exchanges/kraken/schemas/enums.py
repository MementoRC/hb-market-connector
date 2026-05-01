"""Kraken-specific enum types."""

from enum import StrEnum


class _StrValue(StrEnum):
    """Base mixin: str(member) returns the value."""


class KrakenAPITier(_StrValue):
    """Kraken rate-limit tier profiles.

    Maps to the tier keys in ``KRAKEN_RATE_LIMIT_SPEC``.  Higher tiers have
    greater private and matching pool capacity.
    """

    STARTER = "STARTER"
    INTERMEDIATE = "INTERMEDIATE"
    PRO = "PRO"


class KrakenOrderState(_StrValue):
    """Kraken native order status strings.

    Returned by /0/private/OpenOrders, QueryOrders, etc.
    """

    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
