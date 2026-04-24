# tests/unit/test_rest_base.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase


@pytest.fixture
def endpoints() -> dict[str, Endpoint]:
    return {
        "get_book": Endpoint(path="/api/book", method="GET", weight=1, limit=5, window=1.0),
        "place_order": Endpoint(path="/api/orders", method="POST", weight=2, limit=5, window=1.0),
    }


class TestRestConnectorBase:
    @pytest.mark.asyncio
    async def test_successful_request(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(200, json={"status": "ok"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            result = await client.request("get_book")
        assert result == {"status": "ok"}

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
        assert result == {"status": "ok"}
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
