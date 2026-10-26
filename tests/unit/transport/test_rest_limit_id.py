"""Acceptance tests for the explicit ``limit_id`` override on REST requests (#111)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from rate_limit import RateLimit, RateLimiter

from market_connector.exceptions import RateLimitError
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase

BASE = "https://api.example.com"


def _ok() -> httpx.Response:
    return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})


@pytest.fixture
def limiter() -> RateLimiter:
    rl = RateLimiter()
    rl.add_limit(RateLimit("custom", 1, 10.0))
    return rl


@pytest.fixture
def client(limiter: RateLimiter) -> RestConnectorBase:
    endpoints = {"order": Endpoint(path="/api/order", method="POST", limit=5, window=1.0)}
    return RestConnectorBase(BASE, endpoints=endpoints, rate_limiter=limiter)


class TestLimitIdOverride:
    async def test_given_limit_is_reserved_instead_of_lazy_limit(
        self, client: RestConnectorBase, limiter: RateLimiter
    ) -> None:
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            await client.request("order", limit_id="custom")

        assert limiter.blocked_for("custom", {"custom": 1}) > 0
        assert client._registered_limits == set()
        assert not limiter.has_limit("POST:/api/order")

    async def test_429_reports_throttled_on_given_limit(
        self, client: RestConnectorBase, limiter: RateLimiter
    ) -> None:
        with (
            patch.object(client, "_client") as mock_client,
            patch.object(limiter, "report_throttled") as throttled,
        ):
            mock_client.request = AsyncMock(
                return_value=httpx.Response(429, headers={"retry-after": "2"})
            )
            with pytest.raises(RateLimitError):
                await client.request("order", limit_id="custom")

        assert throttled.call_args.args[0] == "custom"

    async def test_unregistered_limit_id_raises_without_sending(
        self, client: RestConnectorBase
    ) -> None:
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=_ok())
            with pytest.raises(ValueError, match="missing"):
                await client.request("order", limit_id="missing")
        mock_client.request.assert_not_called()
