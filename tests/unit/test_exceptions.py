"""Test exception hierarchy for the market connector."""

import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    GatewayError,
    GatewayNotStartedError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)


class TestExceptionHierarchy:
    """All gateway exceptions inherit from GatewayError."""

    @pytest.mark.parametrize(
        "exc_class",
        [
            GatewayNotStartedError,
            OrderRejectedError,
            OrderNotFoundError,
            RateLimitError,
            SubscriptionLimitError,
            AuthenticationError,
            ExchangeUnavailableError,
        ],
    )
    def test_subclass_of_gateway_error(self, exc_class: type) -> None:
        assert issubclass(exc_class, GatewayError)

    @pytest.mark.parametrize(
        "exc_class",
        [
            GatewayError,
            GatewayNotStartedError,
            OrderRejectedError,
            OrderNotFoundError,
            RateLimitError,
            SubscriptionLimitError,
            AuthenticationError,
            ExchangeUnavailableError,
        ],
    )
    def test_subclass_of_exception(self, exc_class: type) -> None:
        assert issubclass(exc_class, Exception)

    def test_message_preserved(self) -> None:
        err = OrderRejectedError("insufficient funds")
        assert str(err) == "insufficient funds"

    def test_gateway_error_catchall(self) -> None:
        """Catching GatewayError catches any subclass."""
        with pytest.raises(GatewayError):
            raise RateLimitError("too many requests")
