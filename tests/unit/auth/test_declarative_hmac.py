"""Tests for DeclarativeRestSigner HMAC mode (spec §6.3, §6.7).

Five test classes covering:
  1. Coinbase HMAC vector  — HMAC-SHA256, hex, ts+method+path+body
  2. Kraken HMAC vector    — HMAC-SHA512, base64, two-stage body_hash
  3. Bybit recv_window     — substituted into both sig input and output header
  4. Binance qs_sorted     — signature in qs_inject, not headers
  5. Monotonic counter     — 10 concurrent sign() calls produce strictly-increasing nonces
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

import pytest

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.auth.protocols import Request
from market_connector.auth.spec import (
    AuthOutputSpec,
    BodyFormat,
    BodyHashSpec,
    HashAlgorithm,
    HmacSigningSpec,
    KeyEncoding,
    KeyMaterialSpec,
    NoncePlacement,
    NonceSource,
    NonceSpec,
    SigAlgorithm,
    SigEncoding,
    SignatureRecipe,
    TimestampFormat,
    TimestampSpec,
    TimestampUnit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    method: str = "POST",
    url: str = "https://api.example.com/v1/orders",
    path: str = "/v1/orders",
    headers: dict | None = None,
    body: str | None = None,
    qs_params: dict | None = None,
) -> Request:
    return Request(
        method=method,
        url=url,
        path=path,
        headers=headers or {},
        body=body,
        qs_params=qs_params or {},
    )


# ---------------------------------------------------------------------------
# 1. Coinbase HMAC vector
# ---------------------------------------------------------------------------


class TestCoinbaseHmac:
    """HMAC-SHA256, hex output, template={ts}{method}{path}{body}."""

    SPEC = HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
        timestamp=TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.INTEGER),
        nonce=NonceSpec(
            source=NonceSource.NONE,
            monotonic=False,
            placement=NoncePlacement.NONE,
            field_name=None,
        ),
        recipe=SignatureRecipe(
            template="{ts}{method}{path}{body}",
            body_format=BodyFormat.NONE,
            body_hash=None,
            algorithm=SigAlgorithm.HMAC_SHA256,
            output_encoding=SigEncoding.HEX,
        ),
        output=AuthOutputSpec(
            headers={
                "CB-ACCESS-KEY": "{api_key}",
                "CB-ACCESS-SIGN": "{sig}",
                "CB-ACCESS-TIMESTAMP": "{ts}",
            },
            body_inject=None,
            qs_inject=None,
        ),
    )

    @pytest.mark.asyncio
    async def test_coinbase_sign_produces_correct_hmac(self):
        api_key = "test-api-key-coinbase"
        secret = "test-secret-coinbase"
        fixed_ts = "1700000000"
        body = '{"size": "1.0", "side": "buy"}'

        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=api_key,
            secret=secret,
            _fixed_ts=fixed_ts,
        )

        request = _make_request(
            method="POST",
            path="/api/v3/orders",
            body=body,
        )
        signed = await signer.sign(request)

        # Compute expected signature the same way the signer must
        message = fixed_ts + "POST" + "/api/v3/orders" + body
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert signed.headers["CB-ACCESS-SIGN"] == expected_sig
        assert signed.headers["CB-ACCESS-KEY"] == api_key
        assert signed.headers["CB-ACCESS-TIMESTAMP"] == fixed_ts

    @pytest.mark.asyncio
    async def test_coinbase_sign_does_not_mutate_input(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key="k",
            secret="s",
            _fixed_ts="1",
        )
        request = _make_request()
        signed = await signer.sign(request)
        assert signed is not request
        assert request.headers == {}  # original untouched


# ---------------------------------------------------------------------------
# 2. Kraken HMAC vector (two-stage: SHA256 inner + HMAC-SHA512 outer)
# ---------------------------------------------------------------------------


class TestKrakenHmac:
    """
    Kraken recipe:
      inner_hash  = SHA256(nonce + post_body)   — bytes
      sig_input   = path_bytes || inner_hash     — bytes concat
      signature   = HMAC-SHA512(base64_secret, sig_input) → base64
    """

    NONCE = "1616492376594"
    POST_BODY = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    API_KEY = "kraken-test-key"
    # A 64-byte base64-encoded secret (Kraken uses 64-byte secrets)
    SECRET_B64 = base64.b64encode(b"K" * 64).decode()

    SPEC = HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.BASE64),
        timestamp=TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER),
        nonce=NonceSpec(
            source=NonceSource.COUNTER,
            monotonic=True,
            placement=NoncePlacement.BODY_FIELD,
            field_name="nonce",
        ),
        recipe=SignatureRecipe(
            template="{path_bytes}{inner_hash}",
            body_format=BodyFormat.FORM_URLENCODED,
            body_hash=BodyHashSpec(
                algorithm=HashAlgorithm.SHA256,
                input_template="{nonce}{body}",
            ),
            algorithm=SigAlgorithm.HMAC_SHA512,
            output_encoding=SigEncoding.BASE64,
        ),
        output=AuthOutputSpec(
            headers={
                "API-Key": "{api_key}",
                "API-Sign": "{sig}",
            },
            body_inject=None,
            qs_inject=None,
        ),
    )

    @pytest.mark.asyncio
    async def test_kraken_sign_produces_correct_signature(self):
        path = "/0/private/AddOrder"
        nonce = self.NONCE
        post_body = self.POST_BODY
        secret_bytes = base64.b64decode(self.SECRET_B64)

        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=self.API_KEY,
            secret=self.SECRET_B64,
            _fixed_nonce=nonce,
        )

        request = _make_request(
            method="POST",
            path=path,
            body=post_body,
        )
        signed = await signer.sign(request)

        # Compute expected:
        # inner = SHA256(nonce_str + post_body)  — raw bytes
        inner_hash = hashlib.sha256((nonce + post_body).encode("utf-8")).digest()
        # outer input = path_bytes || inner_hash bytes
        sig_input = path.encode("utf-8") + inner_hash
        expected_sig = base64.b64encode(
            hmac.new(secret_bytes, sig_input, hashlib.sha512).digest()
        ).decode()

        assert signed.headers["API-Sign"] == expected_sig
        assert signed.headers["API-Key"] == self.API_KEY

    @pytest.mark.asyncio
    async def test_kraken_sign_immutability(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=self.API_KEY,
            secret=self.SECRET_B64,
        )
        request = _make_request(path="/0/private/AddOrder", body=self.POST_BODY)
        original_headers = dict(request.headers)
        await signer.sign(request)
        assert request.headers == original_headers


# ---------------------------------------------------------------------------
# 3. Bybit recv_window — substituted into both sig input and output header
# ---------------------------------------------------------------------------


class TestBybitsRecvWindow:
    """recv_window appears in sig template AND in output header X-BAPI-RECV-WINDOW."""

    RECV_WINDOW = "5000"
    API_KEY = "bybit-key"
    SECRET = "bybit-secret"
    FIXED_TS = "1700001000000"

    SPEC = HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
        timestamp=TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER),
        nonce=NonceSpec(
            source=NonceSource.NONE,
            monotonic=False,
            placement=NoncePlacement.NONE,
            field_name=None,
        ),
        recipe=SignatureRecipe(
            # Bybit: ts + api_key + recv_window + qs_sorted (for GET) or body (for POST)
            template="{ts}{api_key}{recv_window}{body}",
            body_format=BodyFormat.NONE,
            body_hash=None,
            algorithm=SigAlgorithm.HMAC_SHA256,
            output_encoding=SigEncoding.HEX,
        ),
        output=AuthOutputSpec(
            headers={
                "X-BAPI-API-KEY": "{api_key}",
                "X-BAPI-TIMESTAMP": "{ts}",
                "X-BAPI-RECV-WINDOW": "{recv_window}",
                "X-BAPI-SIGN": "{sig}",
            },
            body_inject=None,
            qs_inject=None,
        ),
    )

    @pytest.mark.asyncio
    async def test_recv_window_in_sig_input_and_header(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=self.API_KEY,
            secret=self.SECRET,
            recv_window=self.RECV_WINDOW,
            _fixed_ts=self.FIXED_TS,
        )
        request = _make_request(method="POST", path="/v5/order/create", body="")
        signed = await signer.sign(request)

        # Verify recv_window appears in output header
        assert signed.headers["X-BAPI-RECV-WINDOW"] == self.RECV_WINDOW

        # Verify signature was computed with recv_window in the input
        message = self.FIXED_TS + self.API_KEY + self.RECV_WINDOW + ""
        expected_sig = hmac.new(
            self.SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert signed.headers["X-BAPI-SIGN"] == expected_sig


# ---------------------------------------------------------------------------
# 4. Binance qs_sorted — signature in qs_inject, NOT in headers
# ---------------------------------------------------------------------------


class TestBinanceQsSorted:
    """Binance pattern: sig computed over qs_sorted, injected into qs_inject."""

    API_KEY = "binance-key"
    SECRET = "binance-secret"
    FIXED_TS = "1700002000000"

    SPEC = HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
        timestamp=TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER),
        nonce=NonceSpec(
            source=NonceSource.NONE,
            monotonic=False,
            placement=NoncePlacement.NONE,
            field_name=None,
        ),
        recipe=SignatureRecipe(
            template="{qs_sorted}",
            body_format=BodyFormat.NONE,
            body_hash=None,
            algorithm=SigAlgorithm.HMAC_SHA256,
            output_encoding=SigEncoding.HEX,
        ),
        output=AuthOutputSpec(
            headers={"X-MBX-APIKEY": "{api_key}"},
            body_inject=None,
            qs_inject={"signature": "{sig}"},
        ),
    )

    @pytest.mark.asyncio
    async def test_binance_sig_in_qs_not_headers(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=self.API_KEY,
            secret=self.SECRET,
            _fixed_ts=self.FIXED_TS,
        )
        # Binance GET with query params; qs_sorted = alphabetically-sorted urlencoded params
        qs_params = {"symbol": "BTCUSDT", "timestamp": self.FIXED_TS, "side": "BUY"}
        request = _make_request(
            method="GET",
            path="/api/v3/order",
            qs_params=qs_params,
        )
        signed = await signer.sign(request)

        # Signature must be in qs_params, not in headers
        assert "signature" in signed.qs_params
        header_values = set(signed.headers.values())
        assert not any(len(v) == 64 for v in header_values), (
            "SHA256 hex sig (64 chars) should not appear in headers"
        )

        # Verify the signature value is correct (computed over qs_sorted)
        import urllib.parse

        qs_sorted = urllib.parse.urlencode(sorted(qs_params.items()))
        expected_sig = hmac.new(
            self.SECRET.encode("utf-8"),
            qs_sorted.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert signed.qs_params["signature"] == expected_sig

    @pytest.mark.asyncio
    async def test_binance_no_sig_key_in_headers(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key=self.API_KEY,
            secret=self.SECRET,
            _fixed_ts=self.FIXED_TS,
        )
        request = _make_request(method="GET", path="/api/v3/order")
        signed = await signer.sign(request)
        assert "signature" not in signed.headers


# ---------------------------------------------------------------------------
# 5. Monotonic counter — 10 concurrent sign() calls strictly increasing
# ---------------------------------------------------------------------------


class TestMonotonicCounter:
    """NonceSpec(source=COUNTER, monotonic=True) with asyncio.gather produces
    strictly-increasing nonces across 10 concurrent calls on one signer."""

    SPEC = HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
        timestamp=TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER),
        nonce=NonceSpec(
            source=NonceSource.COUNTER,
            monotonic=True,
            placement=NoncePlacement.HEADER,
            field_name="Nonce",
        ),
        recipe=SignatureRecipe(
            template="{ts}{nonce}{method}{path}",
            body_format=BodyFormat.NONE,
            body_hash=None,
            algorithm=SigAlgorithm.HMAC_SHA256,
            output_encoding=SigEncoding.HEX,
        ),
        output=AuthOutputSpec(
            headers={
                "API-Key": "{api_key}",
                "API-Nonce": "{nonce}",
                "API-Sign": "{sig}",
            },
            body_inject=None,
            qs_inject=None,
        ),
    )

    @pytest.mark.asyncio
    async def test_monotonic_nonces_strictly_increasing(self):
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key="counter-key",
            secret="counter-secret",
        )
        request = _make_request(method="GET", path="/api/v1/balance")

        signed_requests = await asyncio.gather(*[signer.sign(request) for _ in range(10)])

        nonces = [int(r.headers["API-Nonce"]) for r in signed_requests]
        # All 10 nonces must be strictly increasing (no duplicates)
        assert nonces == sorted(nonces), f"Nonces not sorted: {nonces}"
        assert len(set(nonces)) == 10, f"Duplicate nonces found: {nonces}"

    @pytest.mark.asyncio
    async def test_monotonic_nonce_counter_increments_across_calls(self):
        """A single signer's counter must monotonically increase across sequential calls."""
        signer = DeclarativeRestSigner.from_spec(
            self.SPEC,
            api_key="counter-key",
            secret="counter-secret",
        )
        request = _make_request(method="GET", path="/api/v1/balance")

        r1 = await signer.sign(request)
        r2 = await signer.sign(request)
        r3 = await signer.sign(request)

        n1 = int(r1.headers["API-Nonce"])
        n2 = int(r2.headers["API-Nonce"])
        n3 = int(r3.headers["API-Nonce"])
        assert n1 < n2 < n3
