"""Typed exception hierarchy for the exchange gateway framework.

All gateway errors inherit from GatewayError, allowing consumers to catch
either specific errors or the base class as a catch-all.
"""


class GatewayError(Exception):
    """Base class for all gateway errors."""


class GatewayNotStartedError(GatewayError):
    """A gateway method was called before start()."""


class OrderRejectedError(GatewayError):
    """The exchange rejected the order (insufficient funds, invalid params, etc.)."""


class OrderNotFoundError(GatewayError):
    """Cancel or query for an order that does not exist."""


class RateLimitError(GatewayError):
    """REST transport exhausted its rate-limit budget for an endpoint."""


class SubscriptionLimitError(GatewayError):
    """WebSocket subscription cap exceeded for the exchange."""


class AuthenticationError(GatewayError):
    """Credentials are invalid, expired, or missing required permissions."""


class ExchangeUnavailableError(GatewayError):
    """The exchange is down, returning 5xx, or otherwise unreachable."""
