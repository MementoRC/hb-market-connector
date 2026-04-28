"""Tests for market_connector.auth.protocols — Signer Protocol and Request envelope."""

from __future__ import annotations

import dataclasses

import pytest

from market_connector.auth.protocols import Request, Signer

# ---------------------------------------------------------------------------
# Request dataclass
# ---------------------------------------------------------------------------


def test_request_instantiates_with_all_fields():
    req = Request(
        method="GET",
        url="https://api.example.com/v1/orders",
        path="/v1/orders",
        headers={"Content-Type": "application/json"},
        body=None,
        qs_params={"limit": "100"},
    )
    assert req.method == "GET"
    assert req.url == "https://api.example.com/v1/orders"
    assert req.path == "/v1/orders"
    assert req.headers == {"Content-Type": "application/json"}
    assert req.body is None
    assert req.qs_params == {"limit": "100"}


def test_request_body_accepts_bytes():
    req = Request(
        method="POST",
        url="https://api.example.com/v1/orders",
        path="/v1/orders",
        headers={},
        body=b'{"side":"buy"}',
        qs_params={},
    )
    assert req.body == b'{"side":"buy"}'


def test_request_body_accepts_str():
    req = Request(
        method="POST",
        url="https://api.example.com/v1/orders",
        path="/v1/orders",
        headers={},
        body='{"side":"buy"}',
        qs_params={},
    )
    assert req.body == '{"side":"buy"}'


def test_request_is_frozen():
    req = Request(
        method="GET",
        url="https://api.example.com/v1/orders",
        path="/v1/orders",
        headers={},
        body=None,
        qs_params={},
    )
    with pytest.raises((AttributeError, TypeError)):
        req.method = "POST"  # type: ignore[misc]


def test_request_has_exactly_six_fields():
    field_names = {f.name for f in dataclasses.fields(Request)}
    assert field_names == {"method", "url", "path", "headers", "body", "qs_params"}


# ---------------------------------------------------------------------------
# Signer Protocol
# ---------------------------------------------------------------------------


def test_signer_is_runtime_checkable_positive():
    """A class with async def sign(self, request) satisfies Signer structurally."""

    class GoodSigner:
        async def sign(self, request: Request) -> Request:
            return request

    assert isinstance(GoodSigner(), Signer)


def test_signer_is_runtime_checkable_negative():
    """A class without sign() does NOT satisfy Signer."""

    class NotASigner:
        async def authenticate(self, request: Request) -> Request:
            return request

    assert not isinstance(NotASigner(), Signer)


def test_signer_protocol_method_name_is_sign():
    """Signer Protocol exposes a 'sign' attribute."""
    assert hasattr(Signer, "sign")


@pytest.mark.asyncio
async def test_signer_sign_returns_request():
    """Signer.sign takes and returns a Request — exercise via a concrete impl."""

    class EchoSigner:
        async def sign(self, request: Request) -> Request:
            return request

    req = Request(
        method="GET",
        url="https://api.example.com/",
        path="/",
        headers={},
        body=None,
        qs_params={},
    )
    result = await EchoSigner().sign(req)
    assert result is req
