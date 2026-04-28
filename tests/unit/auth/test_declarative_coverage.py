"""Coverage gap tests for market_connector.auth.declarative.

Targets branches not reached by test_declarative_hmac/jwt/bearer:
  - _decode_key:       PEM fallthrough (line 97)
  - _make_timestamp:   NANOSECONDS unit, ISO8601 format, ISO8601_Z format
  - _serialise_body:   bytes body with non-NONE BodyFormat
  - _hash_bytes:       SHA512 branch
  - _from_hmac_spec:   unknown derivation label → ValueError
  - from_spec:         unknown spec type → TypeError
  - sign():            unknown spec type → TypeError (direct __init__ path)
  - _inject_nonce:     QS_FIELD placement
  - _resolve_nested_path: missing key → KeyError
"""

from __future__ import annotations

import re

import pytest

from market_connector.auth.declarative import (
    DeclarativeRestSigner,
    _decode_key,
    _hash_bytes,
    _hmac_sha256_base64,
    _make_timestamp,
    _resolve_nested_path,
    _serialise_body,
)
from market_connector.auth.protocols import Request
from market_connector.auth.spec import (
    AuthOutputSpec,
    BodyFormat,
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

_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _make_request(
    method: str = "GET",
    url: str = "https://api.example.com/v1/test",
    path: str = "/v1/test",
    headers: dict | None = None,
    body: str | bytes | None = None,
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


def _minimal_hmac_spec(
    nonce_source: NonceSource = NonceSource.NONE,
    nonce_placement: NoncePlacement = NoncePlacement.NONE,
    field_name: str | None = None,
    ts_unit: TimestampUnit = TimestampUnit.SECONDS,
    ts_format: TimestampFormat = TimestampFormat.INTEGER,
) -> HmacSigningSpec:
    return HmacSigningSpec(
        key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
        timestamp=TimestampSpec(unit=ts_unit, format=ts_format),
        nonce=NonceSpec(
            source=nonce_source,
            monotonic=False,
            placement=nonce_placement,
            field_name=field_name,
        ),
        recipe=SignatureRecipe(
            template="{ts}{method}{path}",
            body_format=BodyFormat.NONE,
            body_hash=None,
            algorithm=SigAlgorithm.HMAC_SHA256,
            output_encoding=SigEncoding.HEX,
        ),
        output=AuthOutputSpec(
            headers={"X-Sig": "{sig}"},
            body_inject=None,
            qs_inject=None,
        ),
    )


# ---------------------------------------------------------------------------
# _decode_key: PEM fallthrough
# ---------------------------------------------------------------------------


class TestDecodeKeyPemFallthrough:
    """PEM_EC and PEM_RSA encodings fall through to raw UTF-8 bytes."""

    def test_pem_ec_returns_utf8_bytes(self) -> None:
        pem = "-----BEGIN EC PRIVATE KEY-----\nABC\n-----END EC PRIVATE KEY-----\n"
        result = _decode_key(pem, KeyEncoding.PEM_EC)
        assert result == pem.encode("utf-8")

    def test_pem_rsa_returns_utf8_bytes(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nXYZ\n-----END RSA PRIVATE KEY-----\n"
        result = _decode_key(pem, KeyEncoding.PEM_RSA)
        assert result == pem.encode("utf-8")


# ---------------------------------------------------------------------------
# _make_timestamp: uncovered units / formats
# ---------------------------------------------------------------------------


class TestMakeTimestampBranches:
    """NANOSECONDS unit and ISO8601/ISO8601_Z formats are not exercised elsewhere."""

    def test_nanoseconds_unit_returns_large_integer_string(self) -> None:
        result = _make_timestamp(TimestampUnit.NANOSECONDS, TimestampFormat.INTEGER)
        # Nanoseconds since epoch should be > 1e18 after year 2001
        assert int(result) > 1_000_000_000_000_000_000

    def test_iso8601_format_no_z_suffix(self) -> None:
        result = _make_timestamp(TimestampUnit.SECONDS, TimestampFormat.ISO8601)
        assert _ISO8601_RE.match(result), f"Expected ISO8601 without Z, got {result!r}"
        assert not result.endswith("Z")

    def test_iso8601_z_format_has_z_suffix(self) -> None:
        result = _make_timestamp(TimestampUnit.SECONDS, TimestampFormat.ISO8601_Z)
        assert _ISO8601_Z_RE.match(result), f"Expected ISO8601 with Z, got {result!r}"

    def test_milliseconds_nanoseconds_iso8601_z_combination(self) -> None:
        """Cross-product: MILLISECONDS unit with ISO8601_Z format."""
        result = _make_timestamp(TimestampUnit.MILLISECONDS, TimestampFormat.ISO8601_Z)
        assert _ISO8601_Z_RE.match(result), f"Expected ISO8601Z, got {result!r}"


# ---------------------------------------------------------------------------
# _serialise_body: bytes body with non-NONE format
# ---------------------------------------------------------------------------


class TestSerialiseBodyBytes:
    """bytes body with BodyFormat.FORM_URLENCODED hits the bytes-decode branch."""

    def test_bytes_body_decoded_to_str(self) -> None:
        body = b"key=value&other=123"
        result = _serialise_body(body, BodyFormat.FORM_URLENCODED)
        assert result == "key=value&other=123"

    def test_bytes_body_with_json_format(self) -> None:
        body = b'{"x": 1}'
        result = _serialise_body(body, BodyFormat.JSON)
        assert result == '{"x": 1}'


# ---------------------------------------------------------------------------
# _hash_bytes: SHA512 branch
# ---------------------------------------------------------------------------


class TestHashBytesSha512:
    """HashAlgorithm.SHA512 branch in _hash_bytes."""

    def test_sha512_returns_64_byte_digest(self) -> None:
        import hashlib

        data = b"test-data"
        result = _hash_bytes(HashAlgorithm.SHA512, data)
        assert len(result) == 64
        assert result == hashlib.sha512(data).digest()


# ---------------------------------------------------------------------------
# _from_hmac_spec: unknown derivation label → ValueError
# ---------------------------------------------------------------------------


class TestUnknownDerivationLabel:
    """from_spec raises ValueError when derived_credentials contains unknown label."""

    def test_unknown_derivation_fn_raises_value_error(self) -> None:
        spec = HmacSigningSpec(
            key_material=KeyMaterialSpec(
                encoding=KeyEncoding.RAW_STR,
                derived_credentials=(("passphrase", "UNKNOWN_FN_LABEL"),),
            ),
            timestamp=TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.INTEGER),
            nonce=NonceSpec(
                source=NonceSource.NONE,
                monotonic=False,
                placement=NoncePlacement.NONE,
                field_name=None,
            ),
            recipe=SignatureRecipe(
                template="{ts}{method}{path}",
                body_format=BodyFormat.NONE,
                body_hash=None,
                algorithm=SigAlgorithm.HMAC_SHA256,
                output_encoding=SigEncoding.HEX,
            ),
            output=AuthOutputSpec(headers={}, body_inject=None, qs_inject=None),
        )
        with pytest.raises(ValueError, match="Unknown derivation function label"):
            DeclarativeRestSigner.from_spec(spec, api_key="k", secret="s", passphrase="p")


# ---------------------------------------------------------------------------
# from_spec: unknown spec type → TypeError
# ---------------------------------------------------------------------------


class TestFromSpecUnknownType:
    """from_spec raises TypeError when given an unrecognised spec object."""

    def test_unknown_spec_type_raises_type_error(self) -> None:
        class _BogusSpec:
            pass

        with pytest.raises(TypeError, match="Unknown spec type"):
            DeclarativeRestSigner.from_spec(
                _BogusSpec(),  # type: ignore[arg-type]
                api_key="k",
                secret="s",
            )


# ---------------------------------------------------------------------------
# sign(): unknown spec type → TypeError (via __init__ bypass)
# ---------------------------------------------------------------------------


class TestSignUnknownSpecType:
    """sign() raises TypeError when _spec is set to an unrecognised type post-construction."""

    @pytest.mark.asyncio
    async def test_sign_with_injected_unknown_spec_raises_type_error(self) -> None:
        spec = _minimal_hmac_spec()
        signer = DeclarativeRestSigner.from_spec(spec, api_key="k", secret="s", _fixed_ts="1")
        # Replace spec with a bogus object to hit the final raise in sign()
        signer._spec = object()  # type: ignore[assignment]
        with pytest.raises(TypeError, match="Unexpected spec type"):
            await signer.sign(_make_request())


# ---------------------------------------------------------------------------
# _inject_nonce: QS_FIELD placement
# ---------------------------------------------------------------------------


class TestInjectNonceQsField:
    """NoncePlacement.QS_FIELD injects the nonce value into qs_params."""

    @pytest.mark.asyncio
    async def test_qs_field_placement_injects_nonce_into_qs_params(self) -> None:
        spec = _minimal_hmac_spec(
            nonce_source=NonceSource.UUID,
            nonce_placement=NoncePlacement.QS_FIELD,
            field_name="nonce",
        )
        signer = DeclarativeRestSigner.from_spec(
            spec,
            api_key="k",
            secret="s",
            _fixed_ts="12345",
            _fixed_nonce="fixed-nonce-value",
        )
        request = _make_request(qs_params={})
        signed = await signer.sign(request)

        assert signed.qs_params.get("nonce") == "fixed-nonce-value"

    @pytest.mark.asyncio
    async def test_qs_field_placement_does_not_touch_headers(self) -> None:
        spec = _minimal_hmac_spec(
            nonce_source=NonceSource.UUID,
            nonce_placement=NoncePlacement.QS_FIELD,
            field_name="nonce",
        )
        signer = DeclarativeRestSigner.from_spec(
            spec,
            api_key="k",
            secret="s",
            _fixed_ts="1",
            _fixed_nonce="n1",
        )
        signed = await signer.sign(_make_request())
        assert "nonce" not in signed.headers


# ---------------------------------------------------------------------------
# _resolve_nested_path: missing key → KeyError
# ---------------------------------------------------------------------------


class TestResolveNestedPath:
    """_resolve_nested_path raises KeyError when a path segment is absent."""

    def test_missing_top_level_key_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            _resolve_nested_path({"other": "x"}, "missing")

    def test_missing_nested_key_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            _resolve_nested_path({"data": {"x": "1"}}, "data.token")

    def test_valid_nested_path_returns_string(self) -> None:
        result = _resolve_nested_path({"data": {"token": 42}}, "data.token")
        assert result == "42"  # str() is applied

    def test_single_segment_path(self) -> None:
        result = _resolve_nested_path({"access_token": "abc"}, "access_token")
        assert result == "abc"


# ---------------------------------------------------------------------------
# _decode_key: HEX encoding
# ---------------------------------------------------------------------------


class TestDecodeKeyHex:
    """HEX encoding branch in _decode_key."""

    def test_hex_encoded_key_decoded_to_bytes(self) -> None:
        raw_bytes = b"\xde\xad\xbe\xef"
        hex_str = raw_bytes.hex()
        result = _decode_key(hex_str, KeyEncoding.HEX)
        assert result == raw_bytes


# ---------------------------------------------------------------------------
# _hmac_sha256_base64: derivation function body (Kucoin passphrase path)
# ---------------------------------------------------------------------------


class TestHmacSha256Base64:
    """Direct test of the _hmac_sha256_base64 derivation function (lines 74-75)."""

    def test_produces_base64_encoded_hmac(self) -> None:
        import base64
        import hashlib
        import hmac as hmac_mod

        secret = b"test-secret"
        value = "my-passphrase"
        expected = base64.b64encode(
            hmac_mod.new(secret, value.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        result = _hmac_sha256_base64(secret, value)
        assert result == expected


# ---------------------------------------------------------------------------
# _from_hmac_spec: valid derivation label → derived credential computed
# ---------------------------------------------------------------------------


class TestValidDerivationLabel:
    """HMAC_SHA256_BASE64 label produces derived passphrase in derived_creds (lines 302-303)."""

    @pytest.mark.asyncio
    async def test_derived_credential_used_in_signing(self) -> None:
        import base64
        import hashlib
        import hmac as hmac_mod

        secret = "kucoin-secret"
        passphrase_raw = "my-passphrase"

        spec = HmacSigningSpec(
            key_material=KeyMaterialSpec(
                encoding=KeyEncoding.RAW_STR,
                derived_credentials=(("passphrase", "HMAC_SHA256_BASE64"),),
            ),
            timestamp=TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.INTEGER),
            nonce=NonceSpec(
                source=NonceSource.NONE,
                monotonic=False,
                placement=NoncePlacement.NONE,
                field_name=None,
            ),
            recipe=SignatureRecipe(
                template="{ts}{method}{path}",
                body_format=BodyFormat.NONE,
                body_hash=None,
                algorithm=SigAlgorithm.HMAC_SHA256,
                output_encoding=SigEncoding.HEX,
            ),
            output=AuthOutputSpec(
                headers={
                    "KC-API-PASSPHRASE": "{passphrase}",
                    "KC-API-SIGN": "{sig}",
                },
                body_inject=None,
                qs_inject=None,
            ),
        )
        signer = DeclarativeRestSigner.from_spec(
            spec,
            api_key="k",
            secret=secret,
            _fixed_ts="1700000000",
            passphrase=passphrase_raw,
        )
        signed = await signer.sign(_make_request())

        # Derived passphrase must be HMAC-SHA256-base64 of the raw passphrase
        expected_passphrase = base64.b64encode(
            hmac_mod.new(
                secret.encode("utf-8"), passphrase_raw.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode()
        assert signed.headers["KC-API-PASSPHRASE"] == expected_passphrase


# ---------------------------------------------------------------------------
# body_inject: iteration over body_inject fields (lines 435-438)
# ---------------------------------------------------------------------------


class TestBodyInjectIteration:
    """body_inject fields are iterated and templates expanded (lines 435-438)."""

    @pytest.mark.asyncio
    async def test_body_inject_does_not_raise(self) -> None:
        spec = HmacSigningSpec(
            key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
            timestamp=TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.INTEGER),
            nonce=NonceSpec(
                source=NonceSource.NONE,
                monotonic=False,
                placement=NoncePlacement.NONE,
                field_name=None,
            ),
            recipe=SignatureRecipe(
                template="{ts}{method}{path}",
                body_format=BodyFormat.NONE,
                body_hash=None,
                algorithm=SigAlgorithm.HMAC_SHA256,
                output_encoding=SigEncoding.HEX,
            ),
            output=AuthOutputSpec(
                headers={"X-Key": "{api_key}"},
                body_inject={"api_key": "{api_key}", "sig": "{sig}"},
                qs_inject=None,
            ),
        )
        signer = DeclarativeRestSigner.from_spec(
            spec, api_key="test-key", secret="test-secret", _fixed_ts="100"
        )
        # Must complete without error; body_inject is computed but not merged into body
        signed = await signer.sign(_make_request())
        assert signed.headers["X-Key"] == "test-key"


# ---------------------------------------------------------------------------
# _resolve_nonce: NonceSource.UUID and NonceSource.TIMESTAMP (free-running)
# ---------------------------------------------------------------------------


class TestNonceSourceFreeRunning:
    """UUID and TIMESTAMP nonce sources without _fixed_nonce (lines 470, 472)."""

    @pytest.mark.asyncio
    async def test_uuid_nonce_source_produces_uuid_format(self) -> None:
        import re

        uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        spec = _minimal_hmac_spec(
            nonce_source=NonceSource.UUID,
            nonce_placement=NoncePlacement.HEADER,
            field_name="X-Nonce",
        )
        signer = DeclarativeRestSigner.from_spec(spec, api_key="k", secret="s", _fixed_ts="1")
        # No _fixed_nonce → real UUID generated
        signed = await signer.sign(_make_request())
        nonce_val = signed.headers.get("X-Nonce", "")
        assert uuid_re.match(nonce_val), f"Expected UUID format, got {nonce_val!r}"

    @pytest.mark.asyncio
    async def test_timestamp_nonce_source_produces_numeric_string(self) -> None:
        spec = _minimal_hmac_spec(
            nonce_source=NonceSource.TIMESTAMP,
            nonce_placement=NoncePlacement.HEADER,
            field_name="X-Nonce",
        )
        signer = DeclarativeRestSigner.from_spec(spec, api_key="k", secret="s", _fixed_ts="1")
        # No _fixed_nonce → timestamp-derived nonce
        signed = await signer.sign(_make_request())
        nonce_val = signed.headers.get("X-Nonce", "")
        assert nonce_val.isdigit(), f"Expected numeric timestamp nonce, got {nonce_val!r}"
