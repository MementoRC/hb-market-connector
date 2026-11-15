"""UnsupportedOrderError contract (#236): typed, structured, exported, not a builtin lookalike."""

from __future__ import annotations

import pytest

import market_connector
from market_connector import exceptions
from market_connector.exceptions import GatewayError, UnsupportedOrderError


def _make() -> UnsupportedOrderError:
    return UnsupportedOrderError(
        exchange="coinbase",
        order_type="STOP_LOSS",
        feature="trigger.reference=INDEX",
        reason="not offered by venue",
    )


class TestHierarchy:
    def test_is_gateway_error(self) -> None:
        assert issubclass(UnsupportedOrderError, GatewayError)

    def test_not_not_implemented_error(self) -> None:
        assert not issubclass(UnsupportedOrderError, NotImplementedError)

    def test_not_value_error(self) -> None:
        assert not issubclass(UnsupportedOrderError, ValueError)


class TestExports:
    def test_exported_from_package(self) -> None:
        assert market_connector.UnsupportedOrderError is UnsupportedOrderError

    def test_exported_from_exceptions_module(self) -> None:
        assert exceptions.UnsupportedOrderError is UnsupportedOrderError


class TestAttributes:
    def test_structured_attributes(self) -> None:
        err = _make()
        assert err.exchange == "coinbase"
        assert err.order_type == "STOP_LOSS"
        assert err.feature == "trigger.reference=INDEX"
        assert err.reason == "not offered by venue"

    def test_str_is_readable(self) -> None:
        text = str(_make())
        assert "coinbase" in text
        assert "STOP_LOSS" in text
        assert "trigger.reference=INDEX" in text
        assert "not offered by venue" in text

    def test_catchable_as_gateway_error(self) -> None:
        with pytest.raises(GatewayError):
            raise _make()
