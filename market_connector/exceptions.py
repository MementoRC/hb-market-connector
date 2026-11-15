"""Typed exception hierarchy for the exchange gateway framework.

All gateway errors inherit from GatewayError, allowing consumers to catch
either specific errors or the base class as a catch-all.
"""

import data_type_primitives


class GatewayError(Exception):
    """Base class for all gateway errors."""


class GatewayNotStartedError(GatewayError):
    """A gateway method was called before start()."""


class OrderRejectedError(GatewayError):
    """The exchange rejected the order (insufficient funds, invalid params, etc.)."""


class OrderNotFoundError(GatewayError):
    """Cancel or query for an order that does not exist."""


class RateLimitError(GatewayError):
    """REST transport exhausted its rate-limit budget for an endpoint.

    ``retry_after`` is the exchange's (or a conservative) back-off hint in seconds, if any.
    """

    def __init__(self, *args: object, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class RateLimitExceeded(GatewayError):  # noqa: N818
    """A rate-limit pool was exhausted and the request cannot proceed."""


class SubscriptionLimitError(GatewayError):
    """WebSocket subscription cap exceeded for the exchange."""


class AuthenticationError(GatewayError):
    """Credentials are invalid, expired, or missing required permissions."""


class InvalidNonceError(AuthenticationError):
    """The exchange rejected the request nonce. Never retried automatically."""


class ExchangeUnavailableError(GatewayError):
    """The exchange is down, returning 5xx, or otherwise unreachable."""


class OrderOutcomeUnknownError(
    ExchangeUnavailableError, data_type_primitives.OrderOutcomeUnknownError
):
    """The order POST was sent but its outcome is unknown (5xx, Cloudflare, transport error).

    The order may or may not exist on the exchange. It was never re-sent; resolve it by
    looking it up with ``client_order_id``.

    Dual base: ``ExchangeUnavailableError`` keeps existing ``except`` clauses working, and
    ``data_type_primitives.OrderOutcomeUnknownError`` is the L0 type the strategy framework
    catches (ADR-0001). Pickling is handled by the dtp base's ``__reduce__``.
    """

    def __init__(self, message: str | None = None, *, client_order_id: str) -> None:
        # Explicit base call: the dtp __init__ requires client_order_id and is the only
        # __init__ in the MRO, so cooperative super() would not pass it through.
        data_type_primitives.OrderOutcomeUnknownError.__init__(
            self,
            message
            or f"Order outcome unknown for client_order_id={client_order_id!r}: "
            "the response was lost; look the order up by client_order_id before retrying",
            client_order_id=client_order_id,
        )


class ServiceUnavailableError(GatewayError):
    """The exchange service is temporarily unavailable (maintenance, cancel-only mode, etc.)."""


class ExchangeCancelOnlyError(ServiceUnavailableError):
    """The market accepts cancellations only; new orders are rejected."""


class ExchangePostOnlyError(ServiceUnavailableError):
    """The market accepts post-only (maker) orders only."""


class ExchangePermissionError(GatewayError):
    """The request was denied due to insufficient permissions or account restrictions."""


class UnknownPairError(GatewayError):
    """A pair string could not be mapped (no matching rule or fallback)."""

    def __init__(self, pair: str) -> None:
        super().__init__(f"Cannot map pair: {pair!r}")
        self.pair = pair
