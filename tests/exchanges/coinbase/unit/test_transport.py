"""Tests for market_connector.exchanges.coinbase.transport — Phase 1, Step 5."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest

from market_connector.auth.protocols import Request
from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.endpoint import Endpoint

BASE = "https://api.coinbase.com/api/v3"


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


def _client(
    endpoints: dict[str, Endpoint], signer: _FakeSigner
) -> tuple[CoinbaseRestClient, AsyncMock]:
    """Real client through the real base; only the HTTP send is mocked."""
    client = CoinbaseRestClient(base_url=BASE, endpoints=endpoints, signer=signer)
    send = AsyncMock(
        return_value=httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json"}
        )
    )
    client._client.request = send  # type: ignore[method-assign]
    return client, send


class TestCoinbaseRestClient:
    async def test_invokes_signer_with_rest_context(self) -> None:
        """The signer is called once with method, path, query params and body."""
        signer = _FakeSigner(extra_headers={"Authorization": "Bearer test-token"})
        endpoints = {
            "accounts": Endpoint(path="/brokerage/accounts", method="POST", limit=30, window=1.0),
        }
        client, send = _client(endpoints, signer)

        await client.request("accounts", params={"limit": 3}, data={"a": 1})

        assert len(signer.calls) == 1
        req = signer.calls[0]
        assert req.method == "POST"
        assert req.path == "/brokerage/accounts"
        assert req.qs_params == {"limit": 3}
        assert req.body == json.dumps({"a": 1})
        sent = send.call_args.kwargs
        assert sent["headers"]["Authorization"] == "Bearer test-token"
        assert sent["params"] == {"limit": 3}
        await client.close()

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

    async def test_resolves_path_params_before_signing(self) -> None:
        """Path parameters like {order_id} are substituted; signed path equals sent path."""
        signer = _FakeSigner()
        endpoints = {
            "order_status": Endpoint(
                path="/brokerage/orders/historical/{order_id}",
                method="GET",
                limit=30,
                window=1.0,
            ),
        }
        client, send = _client(endpoints, signer)

        await client.request("order_status", params={"order_id": "abc123"})

        assert len(signer.calls) == 1
        signed = signer.calls[0]
        assert signed.path == "/brokerage/orders/historical/abc123"
        assert "{" not in signed.path
        sent = send.call_args.kwargs
        assert urlsplit(sent["url"]).path == f"/api/v3{signed.path}"
        assert sent["params"] is None  # order_id is a path param, not a query param
        await client.close()

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_no_body_signs_none_and_sends_no_content(self, method: str) -> None:
        """data=None: signed body is None; no content= and no Content-Type are sent."""
        signer = _FakeSigner()
        endpoints = {"e": Endpoint(path="/x", method=method, limit=30, window=1.0)}
        client, send = _client(endpoints, signer)

        await client.request("e")

        assert signer.calls[0].body is None
        sent = send.call_args.kwargs
        assert sent["content"] is None
        assert sent["json"] is None
        assert "Content-Type" not in sent["headers"]
        await client.close()

    async def test_post_sent_content_equals_signed_body(self) -> None:
        """The sent content is byte-for-byte the string that was signed."""
        signer = _FakeSigner()
        endpoints = {"e": Endpoint(path="/orders", method="POST", limit=30, window=1.0)}
        client, send = _client(endpoints, signer)

        await client.request("e", data={"product_id": "BTC-USD", "size": "0.1"})

        signed_body = signer.calls[0].body
        assert signed_body is not None
        sent = send.call_args.kwargs
        assert sent["content"] == signed_body
        assert sent["content"].encode() == signed_body.encode()
        assert sent["json"] is None
        assert sent["headers"]["Content-Type"] == "application/json"
        await client.close()
