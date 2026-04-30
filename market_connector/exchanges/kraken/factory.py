"""Signer and WS-auth factories for Kraken API authentication."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.exchanges.kraken.specs import (
    KRAKEN_HMAC_SPEC,
    KRAKEN_PRIVATE_WS_AUTH,
    KRAKEN_PUBLIC_WS_AUTH,
)

if TYPE_CHECKING:
    from market_connector.auth.protocols import Signer
    from market_connector.ws_models.auth_models import PassThroughAuth, TokenFetchAuth


def kraken_signer_factory(api_key: str, secret_key: str) -> Signer:
    """Return a DeclarativeRestSigner configured for Kraken HMAC-SHA512 signing.

    Args:
        api_key:    Kraken API key (public component).
        secret_key: Kraken API secret (base64-encoded private key).

    Returns:
        A Signer instance that produces API-Key and API-Sign headers.

    Raises:
        ValueError: If api_key or secret_key are empty.
    """
    if not api_key:
        raise ValueError("api_key must not be empty")
    if not secret_key:
        raise ValueError("secret_key must not be empty")
    return DeclarativeRestSigner.from_spec(KRAKEN_HMAC_SPEC, api_key=api_key, secret=secret_key)


def kraken_ws_auth_factory(
    rest_client: Any = None,
) -> tuple[PassThroughAuth, TokenFetchAuth]:
    """Return a (public, private) pair of WsAuthModel instances for Kraken.

    Args:
        rest_client: Optional RestClient used by the private TokenFetchAuth to
                     call /0/private/GetWebSocketsToken.  Pass None to create
                     a spec-only instance (e.g. in unit tests).

    Returns:
        A 2-tuple of (KRAKEN_PUBLIC_WS_AUTH, private_ws_auth) where:
        - public_ws_auth  is PassThroughAuth (no-op for public streams)
        - private_ws_auth is TokenFetchAuth with rest_client bound
    """
    private_ws_auth = replace(KRAKEN_PRIVATE_WS_AUTH, rest_client=rest_client)
    return KRAKEN_PUBLIC_WS_AUTH, private_ws_auth


__all__ = [
    "kraken_signer_factory",
    "kraken_ws_auth_factory",
]
