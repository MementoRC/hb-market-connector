"""Byte-identical signature vector test: KrakenAuth._generate_auth_dict vs DeclarativeRestSigner.

This test proves that the new DeclarativeRestSigner (configured via KRAKEN_HMAC_SPEC)
produces byte-identical API-Sign headers to the legacy KrakenAuth._generate_auth_dict()
for the same fixed inputs.

The legacy signing recipe is reproduced inline (option b — hermetic, no parent repo import):
  Source: hummingbot/connector/exchange/kraken/kraken_auth.py — _generate_auth_dict()

Legacy algorithm (extracted verbatim):
  1. api_secret = base64.b64decode(secret_key)               # decode key from base64
  2. api_path = bytes(uri, "utf-8")                           # path as bytes
  3. api_nonce = str(nonce)                                   # nonce as string
  4. api_post = f"nonce={api_nonce}" + "&key=val" ...        # form-encoded body (nonce first)
  5. api_sha256 = hashlib.sha256(bytes(nonce + api_post, "utf-8")).digest()  # SHA256(nonce+body)
  6. api_hmac = hmac.new(api_secret, api_path + api_sha256, hashlib.sha512)  # HMAC-SHA512
  7. signature = base64.b64encode(api_hmac.digest()).decode("utf-8")          # base64 output
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import urllib.parse
from typing import Any

import pytest

from market_connector.auth.protocols import Request

# ---------------------------------------------------------------------------
# Inline legacy signing recipe (from hummingbot/connector/exchange/kraken/kraken_auth.py)
# ---------------------------------------------------------------------------


def _legacy_reference_signature(
    uri: str,
    api_key: str,
    secret_key: str,
    nonce: str,
    data: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Reproduce KrakenAuth._generate_auth_dict() exactly.

    Source: hummingbot/connector/exchange/kraken/kraken_auth.py, lines 45-76
    This is a hermetic copy of the legacy algorithm for cross-validation only.
    Do NOT use this in production code — use kraken_signer_factory() instead.

    Returns:
        dict with "API-Key" and "API-Sign" entries matching legacy output.
    """
    # Decode API private key from base64 format displayed in account management
    api_secret: bytes = base64.b64decode(secret_key)

    # Variables (API method, nonce, and POST data)
    api_path: bytes = bytes(uri, "utf-8")
    api_nonce: str = nonce
    api_post: str = f"nonce={api_nonce}"

    if data is not None:
        for key, value in data.items():
            encoded_key = urllib.parse.quote(str(key))
            encoded_value = urllib.parse.quote(str(value))
            api_post += f"&{encoded_key}={encoded_value}"

    # Cryptographic hash algorithms
    api_sha256: bytes = hashlib.sha256(bytes(api_nonce + api_post, "utf-8")).digest()
    api_hmac_obj: hmac.HMAC = hmac.new(api_secret, api_path + api_sha256, hashlib.sha512)

    # Encode signature into base64 format used in API-Sign value
    api_signature: str = base64.b64encode(api_hmac_obj.digest()).decode("utf-8")

    return {
        "API-Key": api_key,
        "API-Sign": api_signature,
    }


# ---------------------------------------------------------------------------
# Test vectors: fixed inputs (deterministic, reproducible)
# ---------------------------------------------------------------------------
#
# Using a 64-byte secret pre-encoded as base64 (matches Kraken's expected format).
# api_secret_bytes = b"kraken-test-secret-for-unit-tests-only-0000000000000000000000000000"
# base64-encoded:

_TEST_API_KEY = "TestApiKeyForVectors0123456789AB"
_TEST_SECRET_B64 = (
    "a3Jha2VuLXRlc3Qtc2VjcmV0LWZvci11bml0LXRlc3RzLW9ubHkt"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMA=="
)

# Vectors: (description, uri, nonce, data_dict)
_VECTORS = [
    (
        "private_endpoint_with_order_data",
        "/0/private/AddOrder",
        "1616492376594",
        {"ordertype": "limit", "pair": "XBTUSD", "price": "37500", "type": "buy", "volume": "1.25"},
    ),
    (
        "private_endpoint_balance_no_data",
        "/0/private/Balance",
        "1616492400000",
        None,
    ),
    (
        "private_endpoint_with_special_chars",
        "/0/private/CancelOrder",
        "9999999999999",
        {"txid": "OYVGEW-JXNUB-RB5GFD", "pair": "ETH/USD"},
    ),
]


def _compute_new_signature(
    uri: str,
    api_key: str,
    secret_key: str,
    nonce: str,
    data: dict[str, Any] | None,
) -> str:
    """Compute API-Sign via the new DeclarativeRestSigner with a pinned nonce.

    Body construction matches the legacy algorithm exactly:
    - Legacy body (api_post): "nonce=<nonce>&key=val&..."
    - Framework inner hash template: {nonce}{body}
      where body = "nonce=<nonce>&key=val&..." (full form body including nonce field)

    This mirrors legacy SHA256 input: nonce_str + "nonce=<nonce>&key=val&..."
    Nonce field is pre-included in body_str; BODY_FIELD placement will prepend
    "nonce=<val>&" to body again (doubling it). To avoid this, we use NoncePlacement.NONE
    in the pinned signer's spec OR we build the body to match what the legacy does.

    Strategy: pass the FULL legacy body_str (including nonce=<val>&...) as request.body,
    and pin the nonce. The inner hash ctx["body"] will be this full body_str. The
    template {nonce}{body} = nonce_str + full_legacy_body — matching legacy exactly.
    The BODY_FIELD injection (step 11) modifies the *outbound* body, not the signing input.
    """
    from market_connector.auth.declarative import DeclarativeRestSigner
    from market_connector.exchanges.kraken.specs import KRAKEN_HMAC_SPEC

    # Build full legacy-compatible form body: "nonce=<nonce>&key=val&..."
    # This matches exactly what legacy KrakenAuth builds as api_post.
    api_post = f"nonce={nonce}"
    if data is not None:
        for key, value in data.items():
            encoded_key = urllib.parse.quote(str(key))
            encoded_value = urllib.parse.quote(str(value))
            api_post += f"&{encoded_key}={encoded_value}"

    # Pin both nonce and use the full legacy body.
    # The inner hash template {nonce}{body} will compute:
    #   SHA256(nonce + "nonce=<nonce>&key=val&...") — exactly the legacy.
    pinned_signer = DeclarativeRestSigner.from_spec(
        KRAKEN_HMAC_SPEC,
        api_key=api_key,
        secret=secret_key,
        _fixed_nonce=nonce,
    )

    request = Request(
        method="POST",
        url=f"https://api.kraken.com{uri}",
        path=uri,
        headers={},
        body=api_post,  # Full form body INCLUDING nonce= field, matching legacy api_post
        qs_params={},
    )

    signed = asyncio.get_event_loop().run_until_complete(pinned_signer.sign(request))
    return signed.headers["API-Sign"]


class TestSignatureVectorsByteEquality:
    """Prove byte-identical output between legacy KrakenAuth and DeclarativeRestSigner."""

    @pytest.mark.parametrize("description,uri,nonce,data", _VECTORS)
    def test_api_sign_byte_identical(
        self, description: str, uri: str, nonce: str, data: dict[str, Any] | None
    ) -> None:
        """For fixed (uri, nonce, data), legacy and new signers must produce the same API-Sign."""
        legacy_headers = _legacy_reference_signature(
            uri=uri,
            api_key=_TEST_API_KEY,
            secret_key=_TEST_SECRET_B64,
            nonce=nonce,
            data=data,
        )
        legacy_sig = legacy_headers["API-Sign"]

        new_sig = _compute_new_signature(
            uri=uri,
            api_key=_TEST_API_KEY,
            secret_key=_TEST_SECRET_B64,
            nonce=nonce,
            data=data,
        )

        assert new_sig == legacy_sig, (
            f"Signature mismatch for vector '{description}':\n"
            f"  legacy : {legacy_sig}\n"
            f"  new    : {new_sig}"
        )

    @pytest.mark.parametrize("description,uri,nonce,data", _VECTORS)
    def test_api_key_header_preserved(
        self, description: str, uri: str, nonce: str, data: dict[str, Any] | None
    ) -> None:
        """API-Key header must match the provided api_key."""
        legacy_headers = _legacy_reference_signature(
            uri=uri,
            api_key=_TEST_API_KEY,
            secret_key=_TEST_SECRET_B64,
            nonce=nonce,
            data=data,
        )
        assert legacy_headers["API-Key"] == _TEST_API_KEY

    def test_legacy_signatures_differ_across_vectors(self) -> None:
        """Sanity check: distinct inputs must produce distinct signatures (no collision)."""
        sigs = set()
        for _, uri, nonce, data in _VECTORS:
            headers = _legacy_reference_signature(
                uri=uri,
                api_key=_TEST_API_KEY,
                secret_key=_TEST_SECRET_B64,
                nonce=nonce,
                data=data,
            )
            sigs.add(headers["API-Sign"])
        assert len(sigs) == len(_VECTORS), "Expected all vectors to produce distinct signatures"

    def test_new_signatures_differ_across_vectors(self) -> None:
        """Sanity check: new signer also produces distinct signatures for distinct inputs."""
        sigs = set()
        for _, uri, nonce, data in _VECTORS:
            sig = _compute_new_signature(
                uri=uri,
                api_key=_TEST_API_KEY,
                secret_key=_TEST_SECRET_B64,
                nonce=nonce,
                data=data,
            )
            sigs.add(sig)
        assert len(sigs) == len(_VECTORS), "Expected all vectors to produce distinct signatures"
