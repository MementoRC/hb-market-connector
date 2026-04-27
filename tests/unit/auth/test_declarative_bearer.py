"""Tests for DeclarativeRestSigner Bearer mode (spec §6.5).

Two test classes:
  1. BearerTokenFetch  — mock token endpoint → first sign() fetches, subsequent
                         calls within TTL reuse cached token
  2. BearerTtlExpiry   — expired cache triggers re-fetch
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.auth.protocols import Request
from market_connector.auth.spec import BearerTokenSpec

# ---------------------------------------------------------------------------
# Spec fixture
# ---------------------------------------------------------------------------


def _bearer_spec(ttl: int = 300) -> BearerTokenSpec:
    return BearerTokenSpec(
        token_endpoint="https://api.example.com/auth/token",
        token_request_template={"api_key": "{api_key}", "api_secret": "{secret}"},
        token_response_path="data.token",
        token_ttl_seconds=ttl,
        auth_header_name="Authorization",
        auth_header_template="Bearer {token}",
    )


def _make_request() -> Request:
    return Request(
        method="GET",
        url="https://api.example.com/v1/accounts",
        path="/v1/accounts",
        headers={},
        body=None,
        qs_params={},
    )


def _mock_httpx_response(token: str) -> MagicMock:
    """Return a mock httpx.Response-like object that yields the given token."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": {"token": token}}
    return resp


# ---------------------------------------------------------------------------
# Test class 1: token fetch and TTL cache
# ---------------------------------------------------------------------------


class TestBearerTokenFetch:
    """First sign() posts to token endpoint; subsequent calls within TTL reuse cache."""

    @pytest.mark.asyncio
    async def test_first_sign_fetches_token_and_injects_header(self) -> None:
        spec = _bearer_spec()
        signer = DeclarativeRestSigner.from_spec(
            spec, api_key="my-key", secret="my-secret"
        )

        with patch(
            "market_connector.auth.declarative.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_mock_httpx_response("abc-token-123")
            )

            signed = await signer.sign(_make_request())

        assert signed.headers.get("Authorization") == "Bearer abc-token-123"
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_sign_within_ttl_reuses_cached_token(self) -> None:
        spec = _bearer_spec(ttl=300)
        signer = DeclarativeRestSigner.from_spec(
            spec, api_key="my-key", secret="my-secret"
        )

        with patch(
            "market_connector.auth.declarative.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_mock_httpx_response("cached-token")
            )

            signed1 = await signer.sign(_make_request())
            signed2 = await signer.sign(_make_request())

        # Only one POST — second call hit cache
        assert mock_client.post.call_count == 1
        assert signed1.headers["Authorization"] == "Bearer cached-token"
        assert signed2.headers["Authorization"] == "Bearer cached-token"

    @pytest.mark.asyncio
    async def test_token_request_uses_credentials(self) -> None:
        """The POST body must include expanded api_key and secret."""
        spec = _bearer_spec()
        signer = DeclarativeRestSigner.from_spec(
            spec, api_key="KEY123", secret="SEC456"
        )

        with patch(
            "market_connector.auth.declarative.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_mock_httpx_response("tok")
            )

            await signer.sign(_make_request())

        call_kwargs = mock_client.post.call_args
        # The post is called with the token_endpoint URL
        assert call_kwargs.args[0] == "https://api.example.com/auth/token"
        # json body must contain the expanded credential values
        body = call_kwargs.kwargs.get("json", {})
        assert body.get("api_key") == "KEY123"
        assert body.get("api_secret") == "SEC456"


# ---------------------------------------------------------------------------
# Test class 2: TTL expiry triggers re-fetch
# ---------------------------------------------------------------------------


class TestBearerTtlExpiry:
    """After TTL elapses, next sign() must re-fetch the token."""

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_refetch(self) -> None:
        spec = _bearer_spec(ttl=1)  # 1-second TTL for fast expiry test
        signer = DeclarativeRestSigner.from_spec(
            spec, api_key="k", secret="s"
        )

        call_count = 0

        async def _fake_post(url: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return _mock_httpx_response(f"token-{call_count}")

        with patch(
            "market_connector.auth.declarative.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = _fake_post

            signed1 = await signer.sign(_make_request())
            # Manually expire the cache by backdating the fetch time
            signer._bearer_fetched_at -= 2  # move fetched_at 2 s into the past
            signed2 = await signer.sign(_make_request())

        assert signed1.headers["Authorization"] == "Bearer token-1"
        assert signed2.headers["Authorization"] == "Bearer token-2"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_nested_response_path_resolved(self) -> None:
        """token_response_path='result.access_token' works for non-Architect shape."""
        spec = BearerTokenSpec(
            token_endpoint="https://api.ndax.io/auth",
            token_request_template={"api_key": "{api_key}"},
            token_response_path="result.access_token",
            token_ttl_seconds=600,
            auth_header_name="Authorization",
            auth_header_template="Bearer {token}",
        )
        signer = DeclarativeRestSigner.from_spec(spec, api_key="k", secret="s")

        with patch(
            "market_connector.auth.declarative.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"result": {"access_token": "ndax-token"}}
            mock_client.post = AsyncMock(return_value=resp)

            signed = await signer.sign(_make_request())

        assert signed.headers["Authorization"] == "Bearer ndax-token"
