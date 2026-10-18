"""Interactive Brokers domain exceptions."""

from __future__ import annotations

from typing import Any


class IbError(Exception):
    """Base exception for IB-mapped errors."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(f"[IB {error_code}] {message}")
        self.error_code = error_code
        self.message = message


class ContractNotFoundError(IbError):
    """IB error 162 / 200 — no security definition for contract."""


class OrderRejectedError(IbError):
    """IB error 201 / 321 / 325 — order rejected.

    ``advanced_reject`` carries the ib_async ``Trade.advancedError`` text when present.
    """

    def __init__(self, error_code: int, message: str, advanced_reject: str | None = None) -> None:
        super().__init__(error_code, message)
        self.advanced_reject = advanced_reject


class OrderCancelledError(IbError):
    """IB error 202 — order cancelled; ``reason`` is IB's cancellation text."""

    def __init__(self, error_code: int, message: str, reason: str = "") -> None:
        super().__init__(error_code, message)
        self.reason = reason


class RateLimitError(IbError):
    """IB pacing/rate limit: codes 100, 420 and 162 with a "pacing violation" text."""

    def __init__(self, error_code: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(error_code, message)
        self.retry_after = retry_after


class SubscriptionLimitError(IbError):
    """IB error 309 — maximum number of market-depth requests exceeded."""


class ConnectionLostError(IbError):
    """IB error 1100 — connectivity lost."""


class ConnectionTerminatedError(IbError):
    """IB error 1300 — connection terminated, requires explicit reconnect."""


class MarketDataPermissionError(IbError):
    """IB account does not have a subscription for the requested market data.

    IB codes: 354 (not subscribed), 10089 (requires additional subscription).
    """


class HistoricalDataError(IbError):
    """IB historical data request failed or the farm is temporarily disconnected.

    IB codes: 162 (service error), 165 (farm disconnected — transient; caller may retry).
    """


class AmbiguousContractError(Exception):
    """reqContractDetails returned more than one match. NOT an IB error code; a domain decision."""

    def __init__(self, matches: list[Any]) -> None:
        super().__init__(f"reqContractDetails returned {len(matches)} matches; expected 1")
        self.matches = matches
