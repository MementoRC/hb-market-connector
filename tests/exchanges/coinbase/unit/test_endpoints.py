"""Tests for ENDPOINT_REGISTRY — verifies presence, types, and pool assignment."""

from market_connector.exchanges.coinbase.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.coinbase.specs import COINBASE_RATE_LIMIT_SPEC
from market_connector.transport.endpoint import Endpoint


def test_registry_contains_required_endpoints() -> None:
    required = {
        "server_time",
        "products",
        "product_book",
        "candles",
        "accounts",
        "place_order",
        "cancel_orders",
        "list_orders",
        "order_status",
        "order_fills",
        "fee_summary",
    }
    assert required.issubset(ENDPOINT_REGISTRY.keys())


def test_endpoint_is_endpoint_type() -> None:
    for name, ep in ENDPOINT_REGISTRY.items():
        assert isinstance(ep, Endpoint), f"{name} is not an Endpoint"


def test_rate_limits_split_public_private() -> None:
    """Rate-limit pool assignments live in COINBASE_RATE_LIMIT_SPEC, not on Endpoint."""
    ep = COINBASE_RATE_LIMIT_SPEC.endpoint_pools
    # Public endpoints are assigned to the "public" pool (capacity=10).
    assert ep["server_time"] == [("public", 1)]
    # Private endpoints are assigned to the "private" pool (capacity=30).
    assert ep["accounts"] == [("private", 1)]
    assert ep["place_order"] == [("private", 1)]
