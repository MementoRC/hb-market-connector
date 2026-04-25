"""Tests for market_connector.exchanges.coinbase.auth — Phase 1."""

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from market_connector.exchanges.coinbase.auth import (
    _build_jwt,
    _hmac_sign,
    _normalize_pem,
    coinbase_auth,
)


class TestNormalizePem:
    def test_accepts_multiline_pem(self, ec_private_pem: str) -> None:
        result = _normalize_pem(ec_private_pem)
        assert result.startswith("-----BEGIN EC PRIVATE KEY-----")
        assert result.endswith("-----END EC PRIVATE KEY-----")

    def test_accepts_raw_base64(self, ec_private_b64: str) -> None:
        result = _normalize_pem(ec_private_b64)
        assert "-----BEGIN EC PRIVATE KEY-----" in result

    def test_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            _normalize_pem("not a key")


class TestBuildJwt:
    def test_includes_required_claims(self, ec_private_pem: str) -> None:
        token = _build_jwt(
            api_key="test-key", pem=ec_private_pem, uri="GET api.coinbase.com/v3/test"
        )
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        assert decoded["sub"] == "test-key"
        assert decoded["iss"] == "cdp"
        assert decoded["aud"] == ["cdp"]
        assert decoded["uri"] == "GET api.coinbase.com/v3/test"
        assert "nbf" in decoded and "exp" in decoded
        assert decoded["exp"] - decoded["nbf"] == 120

    def test_omits_uri_for_ws(self, ec_private_pem: str) -> None:
        token = _build_jwt(api_key="test-key", pem=ec_private_pem, uri=None)
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        assert "uri" not in decoded

    def test_includes_kid_and_nonce(self, ec_private_pem: str) -> None:
        token = _build_jwt(api_key="test-key", pem=ec_private_pem, uri=None)
        headers = pyjwt.get_unverified_header(token)
        assert headers["kid"] == "test-key"
        assert "nonce" in headers and len(headers["nonce"]) > 0

    def test_es256_signature_round_trip(self, ec_private_pem: str) -> None:
        """ES256 signature must verify against the corresponding public key."""
        token = _build_jwt(
            api_key="test-key", pem=ec_private_pem, uri="GET api.coinbase.com/v3/test"
        )
        private_key = load_pem_private_key(ec_private_pem.encode(), password=None)
        public_key_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        decoded = pyjwt.decode(
            token,
            public_key_pem,
            algorithms=["ES256"],
            audience=["cdp"],
            options={"verify_aud": True},
        )
        assert decoded["sub"] == "test-key"
        assert decoded["iss"] == "cdp"
        assert decoded["aud"] == ["cdp"]


class TestHmacSign:
    def test_produces_hex_digest(self) -> None:
        result = _hmac_sign(secret="secret", message="1234567890GET/v3/orders")
        assert len(result) == 64  # SHA-256 hex = 32 bytes = 64 hex chars
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self) -> None:
        msg = "1234567890GET/v3/orders"
        assert _hmac_sign("secret", msg) == _hmac_sign("secret", msg)
        assert _hmac_sign("secret", msg) != _hmac_sign("secret2", msg)


class TestCoinbaseAuth:
    async def test_rest_jwt_returns_bearer_header(self, ec_private_pem: str) -> None:
        auth = coinbase_auth(api_key="k1", secret_key=ec_private_pem)
        result = await auth(
            {"method": "GET", "path": "/brokerage/orders", "body": "", "context": "rest"}
        )
        assert "Authorization" in result
        assert result["Authorization"].startswith("Bearer ")
        assert result["content-type"] == "application/json"

    async def test_rest_hmac_fallback(self) -> None:
        auth = coinbase_auth(api_key="k1", secret_key="raw_hmac_secret_not_pem")
        result = await auth(
            {"method": "GET", "path": "/brokerage/orders", "body": "", "context": "rest"}
        )
        assert "CB-ACCESS-KEY" in result
        assert "CB-ACCESS-SIGN" in result
        assert "CB-ACCESS-TIMESTAMP" in result

    async def test_ws_jwt_returns_jwt_field(self, ec_private_pem: str) -> None:
        auth = coinbase_auth(api_key="k1", secret_key=ec_private_pem)
        result = await auth({"context": "ws", "channel": "level2", "product_ids": ["BTC-USD"]})
        assert "jwt" in result

    async def test_ws_hmac_fallback(self) -> None:
        auth = coinbase_auth(api_key="k1", secret_key="raw_hmac_secret")
        result = await auth({"context": "ws", "channel": "level2", "product_ids": ["BTC-USD"]})
        assert result["api_key"] == "k1"
        assert "signature" in result
        assert "timestamp" in result
