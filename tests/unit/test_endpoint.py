"""Tests for Endpoint model."""

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
