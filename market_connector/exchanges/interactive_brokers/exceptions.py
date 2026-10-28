"""Interactive Brokers domain exceptions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle


class IbError(Exception):
    """Base exception for IB-mapped errors."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(f"[IB {error_code}] {message}")
        self.error_code = error_code
        self.message = message


class ContractNotFoundError(IbError):
    """IB error 200 — no security definition for contract.

    ``candidates`` holds formatted similar-symbol suggestions (may be empty).
    """

    def __init__(self, error_code: int, message: str, *, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(error_code, message)
        self.candidates = candidates


class OrderRejectedError(IbError):
    """IB error 201 / 321 / 325 — order rejected.

    ``advanced_reject`` carries the ib_async ``Trade.advancedError`` text when present.
    """

    def __init__(self, error_code: int, message: str, advanced_reject: str | None = None) -> None:
        super().__init__(error_code, message)
        self.advanced_reject = advanced_reject


class LinkedOrderRejectedError(OrderRejectedError):
    """A leg of a bracket or OCA group was rejected; the other legs were cancelled.

    ``error_code``/``message`` are the rejected leg's. ``handles`` holds every placed
    leg in placement order; ``cancel_errors`` maps the order id of each sibling whose
    rollback cancel failed to the exception (empty when the rollback was clean).
    """

    def __init__(
        self,
        error_code: int,
        message: str,
        advanced_reject: str | None = None,
        *,
        handles: tuple[OrderHandle, ...] = (),
        cancel_errors: dict[int, Exception] | None = None,
    ) -> None:
        super().__init__(error_code, message, advanced_reject)
        self.handles = handles
        self.cancel_errors = cancel_errors or {}


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


class MarketDataLinesExhaustedError(SubscriptionLimitError):
    """IB error 101 or the local ``market_data_lines`` cap (reported as 101) — no free line."""


class ConnectionLostError(IbError):
    """IB connectivity lost: 1100, 2110 (TWS-to-server), 502 / 504 (API socket)."""


class ConnectionTerminatedError(IbError):
    """IB error 1300 — connection terminated, requires explicit reconnect."""


class MarketDataPermissionError(IbError):
    """IB account does not have a subscription for the requested market data.

    IB codes: 354 (not subscribed), 10089 (requires additional subscription),
    10197 (competing live session), 162 with a "No market data permissions" text.
    """


class MarketDataTimeoutError(IbError):
    """No usable market data (top-of-book or depth) arrived within ``market_data_timeout``."""


class WhatIfTimeoutError(IbError):
    """IB did not answer a what-if order within ``what_if_timeout``."""


class PnlTimeoutError(IbError):
    """IB did not answer a PnL request within ``pnl_timeout``."""


class HistoricalDataError(IbError):
    """IB historical data request failed or the farm is temporarily disconnected.

    IB codes: 162 (service error, unless pacing or permissions), 165 (farm
    disconnected — transient; caller may retry).
    """


class CancelAllError(ExceptionGroup):  # noqa: N818
    """One or more cancels failed in ``cancel_all``; ``cancelled`` holds the successes."""

    cancelled: list[OrderHandle]

    def __new__(
        cls,
        message: str,
        exceptions: Sequence[Exception],
        cancelled: list[OrderHandle] | None = None,
    ) -> Self:
        self = super().__new__(cls, message, exceptions)
        self.cancelled = list(cancelled) if cancelled is not None else []
        return self

    def derive(self, excs: Sequence[Exception], /) -> CancelAllError:  # type: ignore[override]
        return CancelAllError(self.message, excs, self.cancelled)


class IbAccountConfigError(Exception):
    """The configured IB account cannot be used; fix ``IbConnectionSpec.account_id``.

    Raised on connect when several accounts are managed and none is configured,
    or when the configured account is not among the managed ones. NOT an IB error code.
    """


class IbVersionIncompatibleError(Exception):
    """TWS/Gateway refused the connection because of a client/server version mismatch.

    Raised on connect when ib_async reports a version refusal via ``client.apiError``;
    retrying cannot fix it. Pin a compatible Gateway version (see #59). NOT an IB error code.
    """


class AmbiguousContractError(Exception):
    """reqContractDetails returned more than one match. NOT an IB error code; a domain decision."""

    def __init__(
        self,
        matches: list[Any],
        *,
        candidates: tuple[str, ...] = (),
        message: str | None = None,
    ) -> None:
        super().__init__(
            message or f"reqContractDetails returned {len(matches)} matches; expected 1"
        )
        self.matches = matches
        self.candidates = candidates
