"""Tests for ENDPOINT_REGISTRY — verifies presence, types, and rate-limit split."""

from market_connector.exchanges.coinbase.endpoints import ENDPOINT_REGISTRY
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
    # Public: server_time, products, product_book, candles → limit=10
    # Private: accounts, orders, fills, fee_summary → limit=30
    assert ENDPOINT_REGISTRY["server_time"].limit == 10
    assert ENDPOINT_REGISTRY["accounts"].limit == 30
    assert ENDPOINT_REGISTRY["place_order"].limit == 30
