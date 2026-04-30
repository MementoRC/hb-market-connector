"""Shared fixtures for Kraken conformance and contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from market_connector.exchanges.kraken.factory import (
    kraken_signer_factory,
    kraken_ws_auth_factory,
)

# ---------------------------------------------------------------------------
# Test credential constants (mirrors test_signature_vectors.py)
# ---------------------------------------------------------------------------

_TEST_API_KEY = "test_api_key"

# 64-byte secret pre-encoded as base64, matching the format expected by kraken_signer_factory.
# Raw bytes: b"kraken-test-secret-for-unit-tests-only-0000000000000000000000000000"
_TEST_SECRET_B64 = (
    "a3Jha2VuLXRlc3Qtc2VjcmV0LWZvci11bml0LXRlc3RzLW9ubHkt"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMA=="
)

# Fixtures directory root
_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# load_fixture helper
# ---------------------------------------------------------------------------


@pytest.fixture
def load_fixture():
    """Return a callable(category, name) -> dict that loads a JSON fixture."""

    def _load(category: str, name: str) -> Any:
        path = _FIXTURES_DIR / category / name
        with path.open() as fh:
            return json.load(fh)

    return _load


# ---------------------------------------------------------------------------
# kraken_signer
# ---------------------------------------------------------------------------


@pytest.fixture
def kraken_signer():
    """Kraken REST signer bound to test credentials."""
    return kraken_signer_factory(_TEST_API_KEY, _TEST_SECRET_B64)


# ---------------------------------------------------------------------------
# kraken_token_rest_client
# ---------------------------------------------------------------------------


@pytest.fixture
def kraken_token_rest_client():
    """Minimal async REST client stub for token-fetch tests.

    The GET on /0/private/GetWebSocketsToken returns the recorded fixture.
    All other endpoints return an empty success response.
    """
    token_fixture_path = _FIXTURES_DIR / "rest" / "get_websockets_token.json"
    with token_fixture_path.open() as fh:
        token_response = json.load(fh)

    client = AsyncMock()
    client.get = AsyncMock(return_value=token_response)
    client.post = AsyncMock(return_value={"error": [], "result": {}})
    client.put = AsyncMock(return_value={"error": [], "result": {}})
    return client


# ---------------------------------------------------------------------------
# kraken_private_ws_auth
# ---------------------------------------------------------------------------


@pytest.fixture
def kraken_private_ws_auth(kraken_token_rest_client):
    """TokenFetchAuth bound to the stub REST client."""
    _public, private = kraken_ws_auth_factory(rest_client=kraken_token_rest_client)
    return private
