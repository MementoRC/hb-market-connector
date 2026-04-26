"""Tests for Endpoint model."""

from pydantic import BaseModel

from market_connector.transport.endpoint import Endpoint


class TestEndpoint:
    def test_create_with_defaults(self) -> None:
        ep = Endpoint(path="/api/v3/orders", method="POST")
        assert ep.weight == 1
        assert ep.limit == 10
        assert ep.window == 1.0

    def test_create_with_overrides(self) -> None:
        ep = Endpoint(path="/api/v3/book", method="GET", weight=5, limit=20, window=2.0)
        assert ep.weight == 5
        assert ep.limit == 20
        assert ep.window == 2.0

    def test_immutable(self) -> None:
        ep = Endpoint(path="/api/v3/orders", method="POST")
        try:
            ep.weight = 99
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass

    def test_endpoint_response_type_defaults_to_none(self) -> None:
        ep = Endpoint(path="/v1/x", method="GET")
        assert ep.response_type is None

    def test_endpoint_response_type_accepts_pydantic_model(self) -> None:
        class Schema(BaseModel):
            foo: str

        ep = Endpoint(path="/v1/x", method="GET", response_type=Schema)
        assert ep.response_type is Schema
