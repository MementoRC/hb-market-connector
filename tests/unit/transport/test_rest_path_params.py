"""Acceptance tests for resolved path params in REST transport (#110)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx
import pytest

from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request

TEMPLATE = "/api/orders/{order_id}"
BASE = "https://api.example.com/v3"


class _RecordingSigner:
    def __init__(self) -> None:
        self.calls: list[Request] = []

    async def sign(self, request: Request) -> Request:
        self.calls.append(request)
        return replace(request, headers={**request.headers, "X-Sig": "1"})


def _ok() -> httpx.Response:
    return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})


@pytest.fixture
def endpoints() -> dict[str, Endpoint]:
    return {
        "order": Endpoint(path=TEMPLATE, method="GET", limit=5, window=1.0),
        "orders": Endpoint(path="/api/orders", method="GET", limit=5, window=1.0),
        "fills": Endpoint(path="/api/{kind}/{order_id}", method="GET", limit=5, window=1.0),
    }


class TestRestBasePathParams:
    async def test_url_and_signed_path_are_resolved(self, endpoints: dict[str, Endpoint]) -> None:
        signer = _RecordingSigner()
        client = RestConnectorBase(BASE, endpoints=endpoints, signer=signer)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", path_params={"order_id": "abc"})

        assert signer.calls[0].path == "/api/orders/abc"
        assert signer.calls[0].url == f"{BASE}/api/orders/abc"
        assert mock_client.request.call_args.kwargs["url"] == f"{BASE}/api/orders/abc"

    async def test_values_are_percent_encoded(self, endpoints: dict[str, Endpoint]) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", path_params={"order_id": "a/b c?"})

        assert mock_client.request.call_args.kwargs["url"] == f"{BASE}/api/orders/a%2Fb%20c%3F"

    async def test_rate_limit_id_uses_template(self, endpoints: dict[str, Endpoint]) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", path_params={"order_id": "one"})
            await client.request("order", path_params={"order_id": "two"})

        assert client._registered_limits == {f"GET:{TEMPLATE}"}

    async def test_missing_placeholder_value_raises_without_sending(
        self, endpoints: dict[str, Endpoint]
    ) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            with pytest.raises(ValueError, match="order_id"):
                await client.request("fills", path_params={"kind": "x"})
        mock_client.request.assert_not_called()

    async def test_unknown_path_param_raises_without_sending(
        self, endpoints: dict[str, Endpoint]
    ) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            with pytest.raises(ValueError, match="bogus"):
                await client.request("order", path_params={"order_id": "a", "bogus": "b"})
        mock_client.request.assert_not_called()

    async def test_template_without_values_raises_without_sending(
        self, endpoints: dict[str, Endpoint]
    ) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            with pytest.raises(ValueError, match="order_id"):
                await client.request("order", path_params={})
        mock_client.request.assert_not_called()

    async def test_endpoint_without_placeholders_unchanged(
        self, endpoints: dict[str, Endpoint]
    ) -> None:
        client = RestConnectorBase(BASE, endpoints=endpoints)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("orders", params={"limit": 1})

        assert mock_client.request.call_args.kwargs["url"] == f"{BASE}/api/orders"


class TestCoinbaseResolvedUrl:
    async def test_sent_url_is_resolved_and_matches_signed_path(
        self, endpoints: dict[str, Endpoint]
    ) -> None:
        signer = _RecordingSigner()
        client = CoinbaseRestClient(base_url=BASE, endpoints=endpoints, signer=signer)
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", params={"order_id": "abc", "limit": 3})

        kwargs = mock_client.request.call_args.kwargs
        sent_path = urlsplit(kwargs["url"]).path
        assert "{" not in kwargs["url"]
        assert sent_path == "/v3/api/orders/abc"
        assert kwargs["params"] == {"limit": 3}
        assert urlsplit(signer.calls[0].url).path == sent_path
        assert signer.calls[0].path == "/api/orders/abc"
        assert signer.calls[0].path == sent_path.removeprefix("/v3")

    async def test_rate_limit_shared_across_order_ids(self, endpoints: dict[str, Endpoint]) -> None:
        client = CoinbaseRestClient(base_url=BASE, endpoints=endpoints, signer=_RecordingSigner())
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", params={"order_id": "one"})
            await client.request("order", params={"order_id": "two"})

        assert client._registered_limits == {f"GET:{TEMPLATE}"}
