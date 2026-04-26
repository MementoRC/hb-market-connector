# tests/unit/test_rest_base.py
from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import BaseModel

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.response import Response
from market_connector.transport.rest_base import RestConnectorBase


@pytest.fixture
def endpoints() -> dict[str, Endpoint]:
    return {
        "get_book": Endpoint(path="/api/book", method="GET", weight=1, limit=5, window=1.0),
        "place_order": Endpoint(path="/api/orders", method="POST", weight=2, limit=5, window=1.0),
    }


def _make_httpx_response(
    *,
    status_code: int = 200,
    json_body: dict | list | None = None,
    raw_content: bytes | None = None,
    content_type: str | None = "application/json",
) -> httpx.Response:
    headers = {}
    if content_type is not None:
        headers["content-type"] = content_type
    if raw_content is not None:
        return httpx.Response(status_code=status_code, content=raw_content, headers=headers)
    body = b"" if json_body is None else _json.dumps(json_body).encode()
    return httpx.Response(status_code=status_code, content=body, headers=headers)


class TestRestConnectorBase:
    @pytest.mark.asyncio
    async def test_successful_request(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(200, json={"status": "ok"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            result = await client.request("get_book")
        assert isinstance(result, Response)
        assert result.raw == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(200, json={})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            # 5 tokens, weight=2 each -> 2 calls OK (4 tokens), 3rd needs 2 but only 1 left
            await client.request("place_order")
            await client.request("place_order")
            with pytest.raises(RateLimitError):
                await client.request("place_order")

    @pytest.mark.asyncio
    async def test_auth_hook_called(self, endpoints: dict) -> None:
        # AsyncMock wraps the sync lambda — calls it synchronously, returns result as awaited value
        auth_fn = AsyncMock(side_effect=lambda headers: {**headers, "Authorization": "Bearer tok"})
        client = RestConnectorBase(
            base_url="https://api.example.com",
            endpoints=endpoints,
            auth=auth_fn,
        )
        mock_response = httpx.Response(200, json={})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            await client.request("get_book")
            call_kwargs = mock_client.request.call_args
            assert "Authorization" in call_kwargs.kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_retry_on_5xx(self, endpoints: dict) -> None:
        client = RestConnectorBase(
            base_url="https://api.example.com",
            endpoints=endpoints,
            max_retries=2,
            retry_delay=0.01,
        )
        fail = httpx.Response(503, json={"error": "unavailable"})
        success = httpx.Response(200, json={"status": "ok"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(side_effect=[fail, success])
            result = await client.request("get_book")
        assert isinstance(result, Response)
        assert result.raw == {"status": "ok"}
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries(self, endpoints: dict) -> None:
        client = RestConnectorBase(
            base_url="https://api.example.com",
            endpoints=endpoints,
            max_retries=2,
            retry_delay=0.01,
        )
        fail = httpx.Response(503, json={"error": "unavailable"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=fail)
            with pytest.raises(ExchangeUnavailableError):
                await client.request("get_book")

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(401, json={"error": "unauthorized"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            with pytest.raises(AuthenticationError):
                await client.request("get_book")


class _AccountsResp(BaseModel):
    accounts: list[dict]


class TestRequestReturnsResponse:
    @pytest.mark.asyncio
    async def test_returns_response_with_endpoint_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoints = {"list_accounts": Endpoint(path="/v1/accounts", method="GET")}
        client = RestConnectorBase(base_url="https://api.test", endpoints=endpoints)
        fake = _make_httpx_response(json_body={"accounts": []})
        monkeypatch.setattr(client._client, "request", AsyncMock(return_value=fake))

        result = await client.request("list_accounts")
        assert isinstance(result, Response)
        assert result._endpoint == "list_accounts"
        assert result._response_type is None
        assert result.raw == {"accounts": []}
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_response_with_response_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoints = {
            "list_accounts": Endpoint(
                path="/v1/accounts", method="GET", response_type=_AccountsResp
            )
        }
        client = RestConnectorBase(base_url="https://api.test", endpoints=endpoints)
        fake = _make_httpx_response(json_body={"accounts": [{"id": "x"}]})
        monkeypatch.setattr(client._client, "request", AsyncMock(return_value=fake))

        result = await client.request("list_accounts")
        parsed = result.parse()
        assert isinstance(parsed, _AccountsResp)
        assert parsed.accounts == [{"id": "x"}]
        await client.close()

    @pytest.mark.asyncio
    async def test_status_code_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoints = {"x": Endpoint(path="/x", method="GET")}
        client = RestConnectorBase(base_url="https://api.test", endpoints=endpoints)
        fake = _make_httpx_response(status_code=200, json_body={"x": 1})
        monkeypatch.setattr(client._client, "request", AsyncMock(return_value=fake))

        result = await client.request("x")
        assert result.status_code == 200
        await client.close()

    @pytest.mark.asyncio
    async def test_204_returns_response_with_none_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoints = {"cancel": Endpoint(path="/cancel", method="DELETE")}
        client = RestConnectorBase(base_url="https://api.test", endpoints=endpoints)
        fake = _make_httpx_response(status_code=204, raw_content=b"", content_type=None)
        monkeypatch.setattr(client._client, "request", AsyncMock(return_value=fake))

        result = await client.request("cancel")
        assert result.raw is None
        assert result.status_code == 204
        await client.close()
