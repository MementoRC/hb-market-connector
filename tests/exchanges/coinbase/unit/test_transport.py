"""Tests for market_connector.exchanges.coinbase.transport — Phase 1, Step 5."""

import pytest

from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase


class TestCoinbaseRestClient:
    async def test_invokes_signer_with_rest_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CoinbaseRestClient must call the signer with full request context."""
        captured: list[dict] = []

        async def fake_signer(ctx: dict) -> dict[str, str]:
            captured.append(ctx)
            return {"Authorization": "Bearer test-token"}

        endpoints = {
            "accounts": Endpoint(path="/brokerage/accounts", method="GET", limit=30, window=1.0),
        }
        client = CoinbaseRestClient(
            base_url="https://api.coinbase.com/api/v3",
            endpoints=endpoints,
            signer=fake_signer,
        )

        async def fake_parent_request(self, endpoint_name, params=None, data=None, headers=None):  # noqa: ANN001
            return {"headers_received": headers}

        monkeypatch.setattr(RestConnectorBase, "request", fake_parent_request)

        result = await client.request("accounts")

        assert len(captured) == 1
        assert captured[0]["context"] == "rest"
        assert captured[0]["method"] == "GET"
        assert captured[0]["path"] == "/brokerage/accounts"
        assert result["headers_received"]["Authorization"] == "Bearer test-token"

    def test_rejects_non_https_base_url(self) -> None:
        """CoinbaseRestClient must reject http:// base URLs."""
        endpoints = {
            "accounts": Endpoint(path="/brokerage/accounts", method="GET", limit=30, window=1.0),
        }

        async def fake_signer(ctx: dict) -> dict[str, str]:
            return {}

        with pytest.raises(ValueError, match="https://"):
            CoinbaseRestClient(
                base_url="http://api.coinbase.com/api/v3",
                endpoints=endpoints,
                signer=fake_signer,
            )

    async def test_resolves_path_params_before_signing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path parameters like {order_id} are substituted before signing."""
        captured: list[dict] = []

        async def fake_signer(ctx: dict) -> dict[str, str]:
            captured.append(ctx)
            return {}

        endpoints = {
            "order_status": Endpoint(
                path="/brokerage/orders/historical/{order_id}",
                method="GET",
                limit=30,
                window=1.0,
            ),
        }
        client = CoinbaseRestClient(
            base_url="https://api.coinbase.com/api/v3",
            endpoints=endpoints,
            signer=fake_signer,
        )

        async def fake_parent(self, endpoint_name, params=None, data=None, headers=None):  # noqa: ANN001
            return {}

        monkeypatch.setattr(RestConnectorBase, "request", fake_parent)

        await client.request("order_status", params={"order_id": "abc123"})

        assert captured[0]["path"] == "/brokerage/orders/historical/abc123"
