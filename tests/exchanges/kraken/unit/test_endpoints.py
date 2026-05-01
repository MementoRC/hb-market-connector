"""Unit tests for Kraken endpoint registry (Stage 3).

Verifies that all endpoints defined in ENDPOINT_REGISTRY have:
- Correct HTTP method (GET for public, POST for private/matching)
- Correct URL path prefix (/0/public/ or /0/private/)
- Correct rate_limit_pool field (not used by Endpoint dataclass, but
  the registry key names match the Stage 1 KRAKEN_RATE_LIMIT_SPEC pool names)

Note: The Endpoint dataclass does not carry a ``rate_limit_pool`` field —
pool assignment is handled by the rate-limit spec's ``endpoint_pools`` mapping.
The tests here verify the structural contract of the registry (method + path).
"""

from __future__ import annotations

import pytest

from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.transport.endpoint import Endpoint


class TestEndpointRegistry:
    """Verify ENDPOINT_REGISTRY structure and contents."""

    def test_registry_is_dict_of_endpoints(self) -> None:
        assert isinstance(ENDPOINT_REGISTRY, dict)
        for key, ep in ENDPOINT_REGISTRY.items():
            assert isinstance(ep, Endpoint), f"{key!r} value is not an Endpoint"

    # ------------------------------------------------------------------
    # Public endpoints — GET, /0/public/
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "name,expected_path",
        [
            ("asset_pairs", "/0/public/AssetPairs"),
            ("ticker", "/0/public/Ticker"),
            ("depth", "/0/public/Depth"),
            ("server_time", "/0/public/Time"),
        ],
    )
    def test_public_endpoints_get_method(self, name: str, expected_path: str) -> None:
        ep = ENDPOINT_REGISTRY[name]
        assert ep.method == "GET", f"{name}: expected GET, got {ep.method}"
        assert ep.path == expected_path, f"{name}: expected {expected_path}, got {ep.path}"

    def test_public_endpoints_have_public_prefix(self) -> None:
        public_names = {"asset_pairs", "ticker", "depth", "server_time"}
        for name in public_names:
            ep = ENDPOINT_REGISTRY[name]
            assert ep.path.startswith("/0/public/"), (
                f"{name}: expected /0/public/ prefix, got {ep.path}"
            )

    # ------------------------------------------------------------------
    # Private endpoints — POST, /0/private/
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "name,expected_path",
        [
            ("balance", "/0/private/Balance"),
            ("get_websockets_token", "/0/private/GetWebSocketsToken"),
            ("open_orders", "/0/private/OpenOrders"),
            ("query_orders", "/0/private/QueryOrders"),
            ("query_trades", "/0/private/QueryTrades"),
        ],
    )
    def test_private_endpoints_post_method(self, name: str, expected_path: str) -> None:
        ep = ENDPOINT_REGISTRY[name]
        assert ep.method == "POST", f"{name}: expected POST, got {ep.method}"
        assert ep.path == expected_path, f"{name}: expected {expected_path}, got {ep.path}"

    # ------------------------------------------------------------------
    # Matching / trading endpoints — POST, /0/private/
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "name,expected_path",
        [
            ("add_order", "/0/private/AddOrder"),
            ("cancel_order", "/0/private/CancelOrder"),
        ],
    )
    def test_matching_endpoints_post_method(self, name: str, expected_path: str) -> None:
        ep = ENDPOINT_REGISTRY[name]
        assert ep.method == "POST", f"{name}: expected POST, got {ep.method}"
        assert ep.path == expected_path, f"{name}: expected {expected_path}, got {ep.path}"

    # ------------------------------------------------------------------
    # Coverage: all 11 expected endpoints are present
    # ------------------------------------------------------------------

    def test_all_expected_endpoints_present(self) -> None:
        expected = {
            "asset_pairs",
            "ticker",
            "depth",
            "server_time",
            "balance",
            "get_websockets_token",
            "open_orders",
            "query_orders",
            "query_trades",
            "add_order",
            "cancel_order",
        }
        missing = expected - set(ENDPOINT_REGISTRY.keys())
        assert not missing, f"Missing endpoints: {missing}"

    def test_private_endpoints_use_post(self) -> None:
        """All /0/private/ paths must use POST (Kraken API requirement)."""
        for name, ep in ENDPOINT_REGISTRY.items():
            if "/0/private/" in ep.path:
                assert ep.method == "POST", (
                    f"{name}: private endpoint must use POST, got {ep.method}"
                )
