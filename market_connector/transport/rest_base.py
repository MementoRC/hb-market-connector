"""Rate-limited async REST client with retry and auth injection."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.token_bucket import TokenBucket

AuthCallable = Callable[[dict[str, str]], Awaitable[dict[str, str]]]


class RestConnectorBase:
    """Rate-limited REST client with retry and optional auth.

    Args:
        base_url: Base URL for the exchange API.
        endpoints: Mapping of endpoint name -> Endpoint config.
        auth: Async callable that injects auth headers.
        max_retries: Number of retries on transient (5xx) errors.
        retry_delay: Initial delay between retries in seconds (doubles each retry).
    """

    def __init__(
        self,
        base_url: str,
        endpoints: dict[str, Endpoint] | None = None,
        auth: AuthCallable | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoints = endpoints or {}
        self._auth = auth
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient()
        self._buckets: dict[str, TokenBucket] = {}

    def _get_bucket(self, endpoint: Endpoint) -> TokenBucket:
        key = f"{endpoint.method}:{endpoint.path}"
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(rate=endpoint.limit, window=endpoint.window)
        return self._buckets[key]

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a rate-limited, retried request."""
        endpoint = self._endpoints[endpoint_name]
        bucket = self._get_bucket(endpoint)
        await bucket.acquire(weight=endpoint.weight)

        req_headers = dict(headers or {})
        if self._auth is not None:
            req_headers = await self._auth(req_headers)

        url = f"{self._base_url}{endpoint.path}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            response = await self._client.request(
                method=endpoint.method,
                url=url,
                params=params,
                json=data,
                headers=req_headers,
            )

            if response.status_code == 401:
                raise AuthenticationError(f"401 from {endpoint_name}: {response.text}")

            if response.status_code == 429:
                raise RateLimitError(f"429 from {endpoint_name}: exchange-side rate limit")

            if response.status_code >= 500:
                last_error = ExchangeUnavailableError(
                    f"{response.status_code} from {endpoint_name}: {response.text}"
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
                    continue
                raise last_error

            return response.json()

        raise last_error or ExchangeUnavailableError("request failed")

    async def close(self) -> None:
        await self._client.aclose()
