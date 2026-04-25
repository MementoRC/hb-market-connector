"""Authentication utilities for the Coinbase Advanced Trade API."""

from __future__ import annotations

import binascii
import hashlib
import hmac as _hmac_lib
import secrets
import textwrap
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey


def _normalize_pem(secret_key: str) -> str:
    """Normalize secret_key to a valid PEM string.

    Accepts either a full PEM string (with headers and newlines) or a raw
    base64-encoded key body (without headers).  Raises ValueError if the
    input cannot be parsed as a valid EC private key.
    """
    key = secret_key.strip().replace("\\n", "\n")

    if key.startswith("-----") and "\n" in key:
        # Validate it's actually loadable before returning
        serialization.load_pem_private_key(key.encode(), password=None, backend=default_backend())
        return key

    # Strip headers if present but newlines were lost
    key = (
        key.replace("-----BEGIN EC PRIVATE KEY-----", "")
        .replace("-----END EC PRIVATE KEY-----", "")
        .strip()
    )

    try:
        binascii.a2b_base64(key)
    except binascii.Error as exc:
        raise ValueError("The secret key is not a valid base64 string.") from exc

    wrapped = textwrap.wrap(key, width=64)
    pem = "-----BEGIN EC PRIVATE KEY-----\n" + "\n".join(wrapped) + "\n-----END EC PRIVATE KEY-----"
    serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())
    return pem


def _build_jwt(api_key: str, pem: str, uri: str | None = None) -> str:
    """Build an ES256 JWT for Coinbase Advanced Trade API authentication.

    Args:
        api_key: The Coinbase API key name (used as ``sub`` and ``kid``).
        pem: A valid PEM-encoded EC private key string.
        uri: Request URI for REST calls (e.g. ``"GET api.coinbase.com/v3/..."``).
            Pass ``None`` for WebSocket connections — omits the ``uri`` claim.

    Returns:
        A signed JWT string.
    """
    private_key: EllipticCurvePrivateKey = serialization.load_pem_private_key(  # type: ignore[assignment]
        pem.encode(), password=None
    )
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": api_key,
        "iss": "cdp",
        "aud": ["cdp"],
        "nbf": now,
        "exp": now + 120,
    }
    if uri is not None:
        claims["uri"] = uri
    return jwt.encode(
        claims,
        private_key,
        algorithm="ES256",
        headers={"kid": api_key, "nonce": secrets.token_hex()},
    )


def _hmac_sign(secret: str, message: str) -> str:
    """Return the HMAC-SHA256 hex digest of ``message`` keyed with ``secret``."""
    return _hmac_lib.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


_BASE_HOST = "api.coinbase.com"
_USER_AGENT = "hb-coinbase-connector/0.1.0"

AuthCallable = Callable[[dict[str, Any]], Awaitable[dict[str, str]]]


def coinbase_auth(api_key: str, secret_key: str) -> AuthCallable:
    """Return an async AuthCallable. Attempts JWT (EC key); falls back to HMAC.

    The returned callable accepts a context dict and returns auth headers/fields:

    - REST JWT:  ``{"Authorization": "Bearer <token>", "content-type": ..., "User-Agent": ...}``
    - REST HMAC: ``{"CB-ACCESS-KEY": ..., "CB-ACCESS-SIGN": ..., "CB-ACCESS-TIMESTAMP": ..., ...}``
    - WS JWT:    ``{"jwt": "<token>"}``
    - WS HMAC:   ``{"api_key": ..., "signature": ..., "timestamp": ...}``
    """
    try:
        pem: str | None = _normalize_pem(secret_key)
        use_jwt = True
    except ValueError:
        pem = None
        use_jwt = False

    async def _auth(ctx: dict[str, Any]) -> dict[str, str]:
        context = ctx.get("context", "rest")

        if context == "rest":
            method: str = ctx["method"]
            path: str = ctx["path"]
            body: str = ctx.get("body", "")

            if use_jwt:
                uri = f"{method} {_BASE_HOST}{path}"
                token = _build_jwt(api_key, pem, uri=uri)  # type: ignore[arg-type]
                return {
                    "content-type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": _USER_AGENT,
                }
            else:
                ts = str(int(time.time()))
                sig = _hmac_sign(secret_key, ts + method + path + body)
                return {
                    "content-type": "application/json",
                    "CB-ACCESS-KEY": api_key,
                    "CB-ACCESS-SIGN": sig,
                    "CB-ACCESS-TIMESTAMP": ts,
                    "User-Agent": _USER_AGENT,
                }

        if context == "ws":
            if use_jwt:
                return {"jwt": _build_jwt(api_key, pem, uri=None)}  # type: ignore[arg-type]
            ts = str(int(time.time()))
            channel: str = ctx["channel"]
            products: str = ",".join(ctx["product_ids"])
            sig = _hmac_sign(secret_key, ts + channel + products)
            return {"api_key": api_key, "signature": sig, "timestamp": ts}

        raise ValueError(f"Unknown auth context: {context!r}")

    return _auth
