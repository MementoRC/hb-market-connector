"""Signer factory for Coinbase Advanced Trade API authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.exchanges.coinbase._auth_helpers import _try_normalize_pem
from market_connector.exchanges.coinbase.specs import COINBASE_HMAC_SPEC, COINBASE_JWT_SPEC

if TYPE_CHECKING:
    from market_connector.auth.protocols import Signer


def coinbase_signer_factory(api_key: str, secret_key: str) -> Signer:
    """Return the appropriate Signer for the given Coinbase credentials.

    PEM EC secret  → ES256 JWT signer  (CDP API keys).
    Raw string secret → HMAC-SHA256 signer (legacy Exchange API keys).
    """
    pem = _try_normalize_pem(secret_key)
    if pem is not None:
        return DeclarativeRestSigner.from_spec(COINBASE_JWT_SPEC, api_key=api_key, secret=pem)
    return DeclarativeRestSigner.from_spec(COINBASE_HMAC_SPEC, api_key=api_key, secret=secret_key)
