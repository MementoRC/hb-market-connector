"""Tests for transport-layer error types."""

import pytest
from pydantic import BaseModel, ValidationError

from market_connector.exceptions import GatewayError
from market_connector.transport.errors import MarketConnectorParseError


class _Schema(BaseModel):
    foo: str
    bar: int


def _make_validation_error() -> ValidationError:
    try:
        _Schema.model_validate({"foo": 1, "bar": "not-an-int"})
    except ValidationError as e:
        return e
    raise AssertionError("expected ValidationError")


class TestMarketConnectorParseError:
    def test_inherits_gateway_error(self) -> None:
        err = MarketConnectorParseError(
            endpoint="x", raw={}, original=_make_validation_error()
        )
        assert isinstance(err, GatewayError)

    def test_attrs_populated(self) -> None:
        ve = _make_validation_error()
        err = MarketConnectorParseError(endpoint="list_accounts", raw={"a": 1}, original=ve)
        assert err.endpoint == "list_accounts"
        assert err.raw == {"a": 1}
        assert err.original is ve

    def test_message_includes_endpoint(self) -> None:
        ve = _make_validation_error()
        err = MarketConnectorParseError(endpoint="list_accounts", raw={}, original=ve)
        msg = str(err)
        assert "list_accounts" in msg
        assert "validation error" in msg

    def test_truncates_to_three_errors_with_suffix(self) -> None:
        class Multi(BaseModel):
            a: int
            b: int
            c: int
            d: int
            e: int

        try:
            Multi.model_validate({"a": "x", "b": "x", "c": "x", "d": "x", "e": "x"})
        except ValidationError as ve:
            err = MarketConnectorParseError(endpoint="x", raw={}, original=ve)
            msg = str(err)
            assert "(+2 more)" in msg

    def test_errors_passthrough(self) -> None:
        ve = _make_validation_error()
        err = MarketConnectorParseError(endpoint="x", raw={}, original=ve)
        assert err.errors() == ve.errors()

    def test_chain_via_raise_from(self) -> None:
        ve = _make_validation_error()
        with pytest.raises(MarketConnectorParseError) as exc_info:
            try:
                raise ve
            except ValidationError as e:
                raise MarketConnectorParseError(endpoint="x", raw={}, original=e) from e
        assert exc_info.value.__cause__ is ve
