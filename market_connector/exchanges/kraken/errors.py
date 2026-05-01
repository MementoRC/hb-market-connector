"""Kraken error-code mapping and response error raiser.

Kraken REST responses carry a top-level ``error`` field that is a list of
strings (e.g. ``["EOrder:Unknown order"]``).  This module maps those strings
to typed framework exceptions and provides ``raise_on_kraken_error()`` which
should be called after parsing any ``KrakenResponse`` envelope.

Error semantics:
  - Empty list  → success, no exception raised.
  - Known code  → typed framework exception (see ``ERROR_CODE_MAPPING``).
  - Unknown code → ``KrakenAPIError`` (catch-all) containing the raw code list.
"""

from __future__ import annotations

from market_connector.exceptions import (
    AuthenticationError,
    ExchangePermissionError,
    OrderNotFoundError,
    RateLimitError,
    ServiceUnavailableError,
)

# ---------------------------------------------------------------------------
# Kraken-specific catch-all exception
# ---------------------------------------------------------------------------


class KrakenAPIError(Exception):
    """Raised when a Kraken response contains error codes not in ERROR_CODE_MAPPING."""

    def __init__(self, error_codes: list[str]) -> None:
        self.error_codes = error_codes
        super().__init__(f"Kraken API error: {error_codes!r}")


# ---------------------------------------------------------------------------
# Error code → exception type mapping
# ---------------------------------------------------------------------------

ERROR_CODE_MAPPING: dict[str, type[Exception]] = {
    # Order errors
    "EOrder:Unknown order": OrderNotFoundError,
    "EOrder:Rate limit exceeded": RateLimitError,
    "EOrder:Insufficient funds": OrderNotFoundError,
    # API / authentication errors
    "EAPI:Invalid key": AuthenticationError,
    "EAPI:Invalid signature": AuthenticationError,
    "EAPI:Invalid nonce": AuthenticationError,
    "EAPI:Rate limit exceeded": RateLimitError,
    # Permission errors
    "EGeneral:Permission denied": ExchangePermissionError,
    # Service availability errors
    "EService:Unavailable": ServiceUnavailableError,
    "EService:Market in cancel_only mode": ServiceUnavailableError,
    "EService:Busy": ServiceUnavailableError,
}


# ---------------------------------------------------------------------------
# raise_on_kraken_error
# ---------------------------------------------------------------------------


def raise_on_kraken_error(error_codes: list[str]) -> None:
    """Raise a typed exception if ``error_codes`` is non-empty.

    Args:
        error_codes: The ``error`` field from a ``KrakenResponse`` envelope.

    Raises:
        A typed exception from ``ERROR_CODE_MAPPING`` if the first error code
        is a known Kraken error.  ``KrakenAPIError`` if the code is unmapped.
        Nothing if ``error_codes`` is empty.

    Note:
        When multiple errors are present, the first code determines the
        exception type (matching Kraken's documented precedence).  All codes
        are preserved in the exception message or ``KrakenAPIError.error_codes``.
    """
    if not error_codes:
        return

    first_code = error_codes[0]
    exc_type = ERROR_CODE_MAPPING.get(first_code)
    if exc_type is not None:
        raise exc_type(first_code if len(error_codes) == 1 else str(error_codes))

    raise KrakenAPIError(error_codes)
