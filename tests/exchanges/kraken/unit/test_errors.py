"""Unit tests for Kraken error mapping (Stage 3).

Verifies:
- Each known error code in ERROR_CODE_MAPPING raises the correct exception type.
- An empty error list raises nothing (happy path).
- An unmapped error code raises KrakenAPIError.
- Multiple error codes: first code determines the exception type, all codes
  are preserved in the exception.
"""

from __future__ import annotations

import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangePermissionError,
    OrderNotFoundError,
    RateLimitError,
    ServiceUnavailableError,
)
from market_connector.exchanges.kraken.errors import (
    ERROR_CODE_MAPPING,
    KrakenAPIError,
    raise_on_kraken_error,
)


class TestHappyPath:
    def test_empty_error_list_raises_nothing(self) -> None:
        raise_on_kraken_error([])  # must not raise

    def test_single_element_list_raises_nothing(self) -> None:
        """A list with only falsy items (empty strings) raises nothing since `not []` is True."""
        raise_on_kraken_error([])  # no-op


class TestKnownErrorCodes:
    @pytest.mark.parametrize(
        "code,expected_exc",
        [
            ("EOrder:Unknown order", OrderNotFoundError),
            ("EOrder:Rate limit exceeded", RateLimitError),
            ("EAPI:Invalid key", AuthenticationError),
            ("EAPI:Invalid signature", AuthenticationError),
            ("EAPI:Invalid nonce", AuthenticationError),
            ("EAPI:Rate limit exceeded", RateLimitError),
            ("EGeneral:Permission denied", ExchangePermissionError),
            ("EService:Unavailable", ServiceUnavailableError),
            ("EService:Market in cancel_only mode", ServiceUnavailableError),
            ("EService:Busy", ServiceUnavailableError),
        ],
    )
    def test_known_code_raises_correct_type(self, code: str, expected_exc: type) -> None:
        with pytest.raises(expected_exc):
            raise_on_kraken_error([code])

    def test_all_mapping_entries_raise(self) -> None:
        """Sanity-check: every entry in ERROR_CODE_MAPPING actually raises."""
        for code, exc_type in ERROR_CODE_MAPPING.items():
            with pytest.raises(exc_type):
                raise_on_kraken_error([code])


class TestUnmappedErrorCodes:
    def test_unmapped_code_raises_kraken_api_error(self) -> None:
        with pytest.raises(KrakenAPIError) as exc_info:
            raise_on_kraken_error(["EOrder:Some unknown error"])
        assert "EOrder:Some unknown error" in exc_info.value.error_codes

    def test_kraken_api_error_preserves_codes(self) -> None:
        codes = ["EGeneral:UnknownError", "EOrder:AnotherError"]
        with pytest.raises(KrakenAPIError) as exc_info:
            raise_on_kraken_error(codes)
        assert exc_info.value.error_codes == codes

    def test_kraken_api_error_message_contains_codes(self) -> None:
        with pytest.raises(KrakenAPIError) as exc_info:
            raise_on_kraken_error(["EFoo:Bar"])
        assert "EFoo:Bar" in str(exc_info.value)


class TestMultipleErrors:
    def test_first_known_code_determines_type(self) -> None:
        """When first code is known, raise that typed exception (not KrakenAPIError)."""
        with pytest.raises(OrderNotFoundError):
            raise_on_kraken_error(["EOrder:Unknown order", "EService:Busy"])

    def test_first_unknown_code_raises_kraken_api_error(self) -> None:
        with pytest.raises(KrakenAPIError):
            raise_on_kraken_error(["EUnknown:Whatever", "EOrder:Unknown order"])

    def test_multiple_errors_message_contains_list(self) -> None:
        """When multiple errors, the raised exception message includes the full list."""
        with pytest.raises(OrderNotFoundError) as exc_info:
            raise_on_kraken_error(["EOrder:Unknown order", "EService:Busy"])
        # Should contain string representation of the list since len > 1
        assert "EService:Busy" in str(exc_info.value)


class TestErrorCodeMappingCompleteness:
    def test_mapping_has_expected_keys(self) -> None:
        required_keys = {
            "EOrder:Unknown order",
            "EOrder:Rate limit exceeded",
            "EAPI:Invalid key",
            "EAPI:Invalid signature",
            "EAPI:Invalid nonce",
            "EGeneral:Permission denied",
            "EService:Unavailable",
            "EService:Market in cancel_only mode",
        }
        missing = required_keys - set(ERROR_CODE_MAPPING.keys())
        assert not missing, f"Missing required error codes: {missing}"

    def test_kraken_api_error_is_exception(self) -> None:
        err = KrakenAPIError(["EFoo:Bar"])
        assert isinstance(err, Exception)
        assert err.error_codes == ["EFoo:Bar"]
