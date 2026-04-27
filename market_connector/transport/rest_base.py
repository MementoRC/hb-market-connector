"""Rate-limited async REST client with retry and Signer-based auth injection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from market_connector.auth.protocols import Request, Signer
from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.response import Response
from market_connector.transport.token_bucket import TokenBucket

if TYPE_CHECKING:
    from market_connector.transport.endpoint import Endpoint


def _decode_body(raw_response: httpx.Response) -> dict | list | None:
    """Safely decode a JSON body. Returns None for empty (204) or non-JSON responses.

    Content-encoding (gzip, deflate, br) is handled transparently by httpx
    before this function is called — `raw_response.content` is already the
    decompressed bytes.

    Strict media-type matching: `application/json` only. Variants like
    `application/problem+json` (RFC 7807) and `application/json-patch+json`
    are intentionally NOT parsed by default — extend this function or add a
    per-endpoint decoder if/when a connector needs them.
    """
    if not raw_response.content:
        return None
    # Strip parameters (`; charset=utf-8`) before exact match.
    base_type = raw_response.headers.get("content-type", "").split(";")[0].strip().lower()
    if base_type != "application/json":
        return None
    return raw_response.json()


class RestConnectorBase:
    """Rate-limited REST client with retry and optional Signer-based auth.

    Args:
        base_url: Base URL for the exchange API.
        endpoints: Mapping of endpoint name -> Endpoint config.
        signer: Signer Protocol instance that signs a Request envelope.
        max_retries: Number of retries on transient (5xx) errors.
        retry_delay: Initial delay between retries in seconds (doubles each retry).
    """

    def __init__(
        self,
        base_url: str,
        endpoints: dict[str, Endpoint] | None = None,
        signer: Signer | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoints = endpoints or {}
        self._signer = signer
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
    ) -> Response:
        """Execute a rate-limited, retried request.

        Returns a typed Response wrapper with `raw` body decoded by `_decode_body`,
        `status_code`, `headers`, `_endpoint` name, and `_response_type` from the
        endpoint registry.  Call `.parse()` on the result to obtain a validated
        Pydantic model when `response_type` is configured on the endpoint.
        """
        endpoint = self._endpoints[endpoint_name]
        bucket = self._get_bucket(endpoint)
        await bucket.acquire(weight=endpoint.weight)

        url = f"{self._base_url}{endpoint.path}"
        req = Request(
            method=endpoint.method,
            url=url,
            path=endpoint.path,
            headers=dict(headers or {}),
            body=None,
            qs_params=dict(params or {}),
        )
        if self._signer is not None:
            req = await self._signer.sign(req)

        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            response = await self._client.request(
                method=req.method,
                url=url,
                params=req.qs_params or None,
                json=data,
                headers=req.headers,
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

            body = _decode_body(response)
            return Response(
                raw=body,
                status_code=response.status_code,
                headers=response.headers,
                _endpoint=endpoint_name,
                _response_type=endpoint.response_type,
            )

        raise last_error or ExchangeUnavailableError("request failed")

    async def close(self) -> None:
        await self._client.aclose()
