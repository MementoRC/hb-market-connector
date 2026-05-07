"""Tests for IB domain exceptions (Stage 2)."""

from __future__ import annotations

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    AmbiguousContractError,
    ConnectionLostError,
    ConnectionTerminatedError,
    ContractNotFoundError,
    IbError,
    OrderRejectedError,
)


class TestIbError:
    def test_constructs_with_code_and_message(self):
        exc = IbError(200, "No security definition")
        assert exc.error_code == 200
        assert exc.message == "No security definition"
        assert "200" in str(exc)

    def test_is_exception(self):
        assert issubclass(IbError, Exception)


class TestSubclasses:
    @pytest.mark.parametrize(
        "cls,code",
        [
            (ContractNotFoundError, 162),
            (ContractNotFoundError, 200),
            (OrderRejectedError, 201),
            (OrderRejectedError, 321),
            (OrderRejectedError, 325),
            (ConnectionLostError, 1100),
            (ConnectionTerminatedError, 1300),
        ],
    )
    def test_subclass_inherits_ib_error(self, cls, code):
        exc = cls(code, "test message")
        assert isinstance(exc, IbError)
        assert exc.error_code == code
        assert exc.message == "test message"

    def test_can_catch_as_ib_error(self):
        with pytest.raises(IbError):
            raise ContractNotFoundError(162, "no security definition")


class TestAmbiguousContractError:
    def test_carries_matches_list(self):
        matches = ["contractA", "contractB", "contractC"]
        exc = AmbiguousContractError(matches)
        assert exc.matches == matches
        assert "3" in str(exc)

    def test_is_plain_exception_not_ib_error(self):
        exc = AmbiguousContractError([])
        assert isinstance(exc, Exception)
        assert not isinstance(exc, IbError)
