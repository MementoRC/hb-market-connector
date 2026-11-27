"""Review-driven tests for #310: WS JWT auth accepts only P-256 EC keys (ES256)."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth


def _pem(curve: ec.EllipticCurve) -> str:
    key = ec.generate_private_key(curve)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_non_p256_ec_key_rejected() -> None:
    with pytest.raises(ValueError, match="P-256"):
        CoinbaseJwtWsAuth("key", _pem(ec.SECP384R1()))


def test_p256_ec_key_accepted() -> None:
    CoinbaseJwtWsAuth("key", _pem(ec.SECP256R1()))
