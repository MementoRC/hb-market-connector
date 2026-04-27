"""Tests for DeclarativeRestSigner JWT mode (spec §6.4).

Two test classes:
  1. JwtClaimsVector  — known PEM EC key + claims template → decode and verify all fields
  2. JwtNonceUniqueness — {rand_hex} produces a unique nonce per sign() call
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.auth.protocols import Request
from market_connector.auth.spec import (
    JwtAlgorithm,
    JwtSigningSpec,
    KeyEncoding,
    KeyMaterialSpec,
)

# ---------------------------------------------------------------------------
# Test EC key pair (P-256 / ES256) — generated fresh at import time
# ---------------------------------------------------------------------------

_EC_PRIVATE_KEY = generate_private_key(SECP256R1())
_EC_PRIVATE_PEM: str = _EC_PRIVATE_KEY.private_bytes(
    Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
).decode()
_EC_PUBLIC_PEM: bytes = _EC_PRIVATE_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    method: str = "GET",
    url: str = "https://api.coinbase.com/api/v3/brokerage/accounts",
    path: str = "/api/v3/brokerage/accounts",
) -> Request:
    return Request(
        method=method,
        url=url,
        path=path,
        headers={},
        body=None,
        qs_params={},
    )


def _coinbase_jwt_spec() -> JwtSigningSpec:
    return JwtSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.PEM_EC),
        algorithm=JwtAlgorithm.ES256,
        claims={
            "sub": "{api_key}",
            "iss": "cdp",
            "aud": ["public_websocket_api"],
            "uri": "{method} {host}{path}",
        },
        lifetime_seconds=120,
        jwt_headers={"kid": "{api_key}", "nonce": "{rand_hex}"},
        auth_header_name="Authorization",
        auth_header_template="Bearer {jwt}",
    )


# ---------------------------------------------------------------------------
# Test class 1: JWT claims verification
# ---------------------------------------------------------------------------


class TestJwtClaimsVector:
    """Known EC key + claims template → decode and verify all fields."""

    @pytest.fixture
    def spec(self) -> JwtSigningSpec:
        return _coinbase_jwt_spec()

    @pytest.fixture
    def signer(self, spec: JwtSigningSpec) -> DeclarativeRestSigner:
        return DeclarativeRestSigner.from_spec(
            spec,
            api_key="test-api-key-001",
            secret=_EC_PRIVATE_PEM,
        )

    @pytest.mark.asyncio
    async def test_authorization_header_set(
        self, signer: DeclarativeRestSigner
    ) -> None:
        request = _make_request()
        before = int(time.time())
        signed = await signer.sign(request)
        after = int(time.time())

        auth = signed.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"Expected 'Bearer <token>', got: {auth!r}"

        token = auth[len("Bearer "):]
        # Decode without verification to inspect claims (key is test-only)
        payload = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["ES256"],
        )
        header = jwt.get_unverified_header(token)

        # Claims
        assert payload["sub"] == "test-api-key-001"
        assert payload["iss"] == "cdp"
        assert payload["aud"] == ["public_websocket_api"]
        assert payload["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"

        # nbf / exp window — nbf is int(time.time()), so compare with int bounds
        assert before <= payload["nbf"] <= after
        assert payload["exp"] == payload["nbf"] + 120

        # JWT headers
        assert header["alg"] == "ES256"
        assert header["kid"] == "test-api-key-001"
        assert "nonce" in header

    @pytest.mark.asyncio
    async def test_jwt_verifies_with_public_key(
        self, signer: DeclarativeRestSigner
    ) -> None:
        """Token must be verifiable using the corresponding EC public key."""
        request = _make_request()
        signed = await signer.sign(request)
        token = signed.headers["Authorization"][len("Bearer "):]

        # Full verification using the module-level public key — should not raise
        payload = jwt.decode(
            token,
            _EC_PUBLIC_PEM,
            algorithms=["ES256"],
            audience=["public_websocket_api"],
        )
        assert payload["iss"] == "cdp"


# ---------------------------------------------------------------------------
# Test class 2: rand_hex nonce uniqueness
# ---------------------------------------------------------------------------


class TestJwtNonceUniqueness:
    """{rand_hex} in jwt_headers produces a unique nonce per sign() call."""

    @pytest.mark.asyncio
    async def test_sequential_calls_produce_different_nonces(self) -> None:
        spec = _coinbase_jwt_spec()
        signer = DeclarativeRestSigner.from_spec(
            spec,
            api_key="key",
            secret=_EC_PRIVATE_PEM,
        )
        request = _make_request()

        signed1 = await signer.sign(request)
        signed2 = await signer.sign(request)

        token1 = signed1.headers["Authorization"][len("Bearer "):]
        token2 = signed2.headers["Authorization"][len("Bearer "):]

        header1 = jwt.get_unverified_header(token1)
        header2 = jwt.get_unverified_header(token2)

        assert header1["nonce"] != header2["nonce"], (
            f"Expected unique nonces per call, got same: {header1['nonce']!r}"
        )
