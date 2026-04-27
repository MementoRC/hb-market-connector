"""Private authentication helpers for Coinbase Advanced Trade API."""

from __future__ import annotations

import binascii
import textwrap

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization


def _try_normalize_pem(secret_key: str) -> str | None:
    """Return a normalized PEM string if secret_key looks like an EC private key, else None.

    Accepts:
    - A full PEM string (with headers and real newlines)
    - A PEM string where newlines are literal ``\\n`` escape sequences
    - A raw base64-encoded EC key body (without headers)

    Returns ``None`` for any input that is not PEM-shaped or cannot be parsed
    as a valid EC private key (e.g. a raw HMAC secret string).
    """
    if not secret_key:
        return None

    key = secret_key.strip().replace("\\n", "\n")

    if key.startswith("-----") and "\n" in key:
        try:
            serialization.load_pem_private_key(key.encode(), password=None, backend=default_backend())
            return key
        except Exception:
            return None

    # Strip headers if present but newlines were lost
    stripped = (
        key.replace("-----BEGIN EC PRIVATE KEY-----", "")
        .replace("-----END EC PRIVATE KEY-----", "")
        .strip()
    )

    try:
        binascii.a2b_base64(stripped)
    except (binascii.Error, Exception):
        return None

    wrapped = textwrap.wrap(stripped, width=64)
    pem = "-----BEGIN EC PRIVATE KEY-----\n" + "\n".join(wrapped) + "\n-----END EC PRIVATE KEY-----"

    try:
        serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())
        return pem
    except Exception:
        return None
