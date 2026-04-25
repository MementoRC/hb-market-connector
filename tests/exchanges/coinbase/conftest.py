"""Shared test fixtures for market_connector.exchanges.coinbase tests."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key


@pytest.fixture
def ec_private_pem() -> str:
    key = generate_private_key(SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def ec_private_b64(ec_private_pem: str) -> str:
    body = ec_private_pem
    body = body.replace("-----BEGIN EC PRIVATE KEY-----", "")
    body = body.replace("-----END EC PRIVATE KEY-----", "")
    return body.strip().replace("\n", "")
