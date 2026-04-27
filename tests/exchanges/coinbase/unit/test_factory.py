"""Tests for market_connector.exchanges.coinbase.factory — Stage 3 Task 15."""

from __future__ import annotations

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.auth.spec import HmacSigningSpec, JwtSigningSpec
from market_connector.exchanges.coinbase.factory import coinbase_signer_factory
from market_connector.exchanges.coinbase.specs import COINBASE_HMAC_SPEC, COINBASE_JWT_SPEC


class TestCoinbaseSignerFactory:
    def test_pem_secret_returns_signer(self, ec_private_pem: str) -> None:
        signer = coinbase_signer_factory(api_key="k1", secret_key=ec_private_pem)
        assert isinstance(signer, DeclarativeRestSigner)

    def test_pem_secret_uses_jwt_spec(self, ec_private_pem: str) -> None:
        signer = coinbase_signer_factory(api_key="k1", secret_key=ec_private_pem)
        assert isinstance(signer._spec, JwtSigningSpec)
        assert signer._spec is COINBASE_JWT_SPEC

    def test_raw_secret_returns_signer(self) -> None:
        signer = coinbase_signer_factory(api_key="k1", secret_key="raw_hmac_secret_not_pem")
        assert isinstance(signer, DeclarativeRestSigner)

    def test_raw_secret_uses_hmac_spec(self) -> None:
        signer = coinbase_signer_factory(api_key="k1", secret_key="raw_hmac_secret_not_pem")
        assert isinstance(signer._spec, HmacSigningSpec)
        assert signer._spec is COINBASE_HMAC_SPEC

    async def test_es256_round_trip(self, ec_private_pem: str) -> None:
        """ES256 JWT produced by the factory signer must verify against the public key."""
        from market_connector.auth.protocols import Request

        signer = coinbase_signer_factory(api_key="test-key", secret_key=ec_private_pem)
        request = Request(
            method="GET",
            url="https://api.coinbase.com/api/v3/brokerage/accounts",
            path="/api/v3/brokerage/accounts",
            headers={},
            body=None,
            qs_params={},
        )
        signed = await signer.sign(request)

        auth_header = signed.headers.get("Authorization", "")
        assert auth_header.startswith("Bearer ")
        token = auth_header[len("Bearer ") :]

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
