"""Tests for market_connector.exchanges.coinbase.transport — Phase 1, Step 5."""

from dataclasses import replace

import pytest

from market_connector.auth.protocols import Request
from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase


class _FakeSigner:
    """Minimal Signer Protocol implementation for tests."""

    def __init__(self, extra_headers: dict[str, str] | None = None) -> None:
        self.calls: list[Request] = []
        self._extra_headers = extra_headers or {}

    async def sign(self, request: Request) -> Request:
        self.calls.append(request)
        merged = dict(request.headers)
        merged.update(self._extra_headers)
        return replace(request, headers=merged)


class TestCoinbaseRestClient:
    async def test_invokes_signer_with_rest_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CoinbaseRestClient must call the signer with full request context."""
        signer = _FakeSigner(extra_headers={"Authorization": "Bearer test-token"})

        endpoints = {
            "accounts": Endpoint(path="/brokerage/accounts", method="GET", limit=30, window=1.0),
        }
        client = CoinbaseRestClient(
            base_url="https://api.coinbase.com/api/v3",
            endpoints=endpoints,
            signer=signer,
        )

        captured_headers: list[dict] = []

        async def fake_parent_request(self, endpoint_name, params=None, data=None, headers=None):  # noqa: ANN001
            captured_headers.append(headers or {})
            return {"headers_received": headers}

        monkeypatch.setattr(RestConnectorBase, "request", fake_parent_request)

        await client.request("accounts")

        assert len(signer.calls) == 1
        req = signer.calls[0]
        assert req.method == "GET"
        assert req.path == "/brokerage/accounts"
        assert captured_headers[0].get("Authorization") == "Bearer test-token"

    def test_rejects_non_https_base_url(self) -> None:
        """CoinbaseRestClient must reject http:// base URLs."""
        endpoints = {
            "accounts": Endpoint(path="/brokerage/accounts", method="GET", limit=30, window=1.0),
        }
        signer = _FakeSigner()
        with pytest.raises(ValueError, match="https://"):
            CoinbaseRestClient(
                base_url="http://api.coinbase.com/api/v3",
                endpoints=endpoints,
                signer=signer,
            )

    async def test_resolves_path_params_before_signing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path parameters like {order_id} are substituted before signing."""
        signer = _FakeSigner()

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
            signer=signer,
        )

        async def fake_parent(self, endpoint_name, params=None, data=None, headers=None):  # noqa: ANN001
            return {}

        monkeypatch.setattr(RestConnectorBase, "request", fake_parent)

        await client.request("order_status", params={"order_id": "abc123"})

        assert len(signer.calls) == 1
        assert signer.calls[0].path == "/brokerage/orders/historical/abc123"
