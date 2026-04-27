# tests/unit/auth/test_spec.py
"""Tests for SigningSpec discriminated union dataclasses (spec §6.3–6.5)."""

from __future__ import annotations

import pytest

from market_connector.auth.spec import (
    AuthOutputSpec,
    BearerTokenSpec,
    BodyFormat,
    BodyHashSpec,
    HashAlgorithm,
    HmacSigningSpec,
    JwtAlgorithm,
    JwtSigningSpec,
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
    TokenInjectStrategy,
)

# ---------------------------------------------------------------------------
# Fixtures — valid sub-spec building blocks
# ---------------------------------------------------------------------------


@pytest.fixture
def key_material_raw() -> KeyMaterialSpec:
    return KeyMaterialSpec(encoding=KeyEncoding.RAW_STR)


@pytest.fixture
def timestamp_ms_int() -> TimestampSpec:
    return TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER)


@pytest.fixture
def nonce_none() -> NonceSpec:
    return NonceSpec(
        source=NonceSource.NONE,
        monotonic=False,
        placement=NoncePlacement.NONE,
        field_name=None,
    )


@pytest.fixture
def nonce_header() -> NonceSpec:
    return NonceSpec(
        source=NonceSource.UUID,
        monotonic=False,
        placement=NoncePlacement.HEADER,
        field_name="X-Nonce",
    )


@pytest.fixture
def signature_recipe_basic() -> SignatureRecipe:
    return SignatureRecipe(
        template="{ts}{method}{path}{body}",
        body_format=BodyFormat.JSON,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.HEX,
    )


@pytest.fixture
def auth_output_headers_only() -> AuthOutputSpec:
    return AuthOutputSpec(
        headers={"X-API-Key": "{api_key}", "X-Signature": "{sig}"},
        body_inject=None,
        qs_inject=None,
    )


# ---------------------------------------------------------------------------
# HmacSigningSpec — full composition
# ---------------------------------------------------------------------------


class TestHmacSigningSpec:
    def test_construct_valid_full_composition(
        self,
        key_material_raw: KeyMaterialSpec,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        signature_recipe_basic: SignatureRecipe,
        auth_output_headers_only: AuthOutputSpec,
    ) -> None:
        spec = HmacSigningSpec(
            key_material=key_material_raw,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=signature_recipe_basic,
            output=auth_output_headers_only,
        )
        assert spec.key_material.encoding == KeyEncoding.RAW_STR
        assert spec.timestamp.unit == TimestampUnit.MILLISECONDS
        assert spec.nonce.source == NonceSource.NONE
        assert spec.recipe.algorithm == SigAlgorithm.HMAC_SHA256
        assert spec.output.headers == {"X-API-Key": "{api_key}", "X-Signature": "{sig}"}

    def test_frozen_immutable(
        self,
        key_material_raw: KeyMaterialSpec,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        signature_recipe_basic: SignatureRecipe,
        auth_output_headers_only: AuthOutputSpec,
    ) -> None:
        spec = HmacSigningSpec(
            key_material=key_material_raw,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=signature_recipe_basic,
            output=auth_output_headers_only,
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.recipe = signature_recipe_basic  # type: ignore[misc]

    def test_isinstance_discrimination(
        self,
        key_material_raw: KeyMaterialSpec,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        signature_recipe_basic: SignatureRecipe,
        auth_output_headers_only: AuthOutputSpec,
    ) -> None:
        spec = HmacSigningSpec(
            key_material=key_material_raw,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=signature_recipe_basic,
            output=auth_output_headers_only,
        )
        assert isinstance(spec, HmacSigningSpec)
        assert not isinstance(spec, JwtSigningSpec)
        assert not isinstance(spec, BearerTokenSpec)

    def test_with_body_hash_spec(
        self,
        key_material_raw: KeyMaterialSpec,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        auth_output_headers_only: AuthOutputSpec,
    ) -> None:
        body_hash = BodyHashSpec(
            algorithm=HashAlgorithm.SHA256,
            input_template="{nonce}{body}",
        )
        recipe = SignatureRecipe(
            template="{path_bytes}{inner_hash}",
            body_format=BodyFormat.FORM_URLENCODED,
            body_hash=body_hash,
            algorithm=SigAlgorithm.HMAC_SHA512,
            output_encoding=SigEncoding.BASE64,
        )
        spec = HmacSigningSpec(
            key_material=key_material_raw,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=recipe,
            output=auth_output_headers_only,
        )
        assert spec.recipe.body_hash is not None
        assert spec.recipe.body_hash.algorithm == HashAlgorithm.SHA256

    def test_with_derived_credentials(
        self,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        signature_recipe_basic: SignatureRecipe,
        auth_output_headers_only: AuthOutputSpec,
    ) -> None:
        # KeyMaterialSpec with derived_credentials (Kucoin passphrase pattern)
        km = KeyMaterialSpec(
            encoding=KeyEncoding.BASE64,
            derived_credentials=(("passphrase", "HMAC_SHA256_BASE64"),),
        )
        spec = HmacSigningSpec(
            key_material=km,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=signature_recipe_basic,
            output=auth_output_headers_only,
        )
        assert spec.key_material.encoding == KeyEncoding.BASE64
        assert len(spec.key_material.derived_credentials) == 1

    def test_qs_inject_output(
        self,
        key_material_raw: KeyMaterialSpec,
        timestamp_ms_int: TimestampSpec,
        nonce_none: NonceSpec,
        signature_recipe_basic: SignatureRecipe,
    ) -> None:
        output = AuthOutputSpec(
            headers={"X-API-Key": "{api_key}"},
            body_inject=None,
            qs_inject={"signature": "{sig}"},
        )
        spec = HmacSigningSpec(
            key_material=key_material_raw,
            timestamp=timestamp_ms_int,
            nonce=nonce_none,
            recipe=signature_recipe_basic,
            output=output,
        )
        assert spec.output.qs_inject == {"signature": "{sig}"}


# ---------------------------------------------------------------------------
# KeyMaterialSpec
# ---------------------------------------------------------------------------


class TestKeyMaterialSpec:
    def test_all_key_encodings(self) -> None:
        for enc in KeyEncoding:
            km = KeyMaterialSpec(encoding=enc)
            assert km.encoding == enc

    def test_default_derived_credentials_empty(self) -> None:
        km = KeyMaterialSpec(encoding=KeyEncoding.RAW_STR)
        assert km.derived_credentials == ()


# ---------------------------------------------------------------------------
# TimestampSpec
# ---------------------------------------------------------------------------


class TestTimestampSpec:
    def test_all_combinations(self) -> None:
        for unit in TimestampUnit:
            for fmt in TimestampFormat:
                ts = TimestampSpec(unit=unit, format=fmt)
                assert ts.unit == unit
                assert ts.format == fmt


# ---------------------------------------------------------------------------
# NonceSpec — cross-field validation
# ---------------------------------------------------------------------------


class TestNonceSpec:
    def test_valid_none_placement_no_field_name(self) -> None:
        spec = NonceSpec(
            source=NonceSource.NONE,
            monotonic=False,
            placement=NoncePlacement.NONE,
            field_name=None,
        )
        assert spec.placement == NoncePlacement.NONE

    def test_valid_sig_only_no_field_name(self) -> None:
        spec = NonceSpec(
            source=NonceSource.TIMESTAMP,
            monotonic=False,
            placement=NoncePlacement.SIG_ONLY,
            field_name=None,
        )
        assert spec.placement == NoncePlacement.SIG_ONLY

    def test_valid_header_with_field_name(self) -> None:
        spec = NonceSpec(
            source=NonceSource.UUID,
            monotonic=False,
            placement=NoncePlacement.HEADER,
            field_name="X-Nonce",
        )
        assert spec.field_name == "X-Nonce"

    def test_valid_body_field_with_field_name(self) -> None:
        spec = NonceSpec(
            source=NonceSource.COUNTER,
            monotonic=True,
            placement=NoncePlacement.BODY_FIELD,
            field_name="nonce",
        )
        assert spec.field_name == "nonce"

    def test_valid_qs_field_with_field_name(self) -> None:
        spec = NonceSpec(
            source=NonceSource.COUNTER,
            monotonic=True,
            placement=NoncePlacement.QS_FIELD,
            field_name="nonce",
        )
        assert spec.field_name == "nonce"

    def test_body_field_placement_without_field_name_raises(self) -> None:
        with pytest.raises(ValueError, match="field_name"):
            NonceSpec(
                source=NonceSource.COUNTER,
                monotonic=True,
                placement=NoncePlacement.BODY_FIELD,
                field_name=None,
            )

    def test_header_placement_without_field_name_raises(self) -> None:
        with pytest.raises(ValueError, match="field_name"):
            NonceSpec(
                source=NonceSource.UUID,
                monotonic=False,
                placement=NoncePlacement.HEADER,
                field_name=None,
            )

    def test_qs_field_placement_without_field_name_raises(self) -> None:
        with pytest.raises(ValueError, match="field_name"):
            NonceSpec(
                source=NonceSource.TIMESTAMP,
                monotonic=False,
                placement=NoncePlacement.QS_FIELD,
                field_name=None,
            )


# ---------------------------------------------------------------------------
# JwtSigningSpec
# ---------------------------------------------------------------------------


class TestJwtSigningSpec:
    def test_construct_valid(self) -> None:
        km = KeyMaterialSpec(encoding=KeyEncoding.PEM_EC)
        spec = JwtSigningSpec(
            key_material=km,
            algorithm=JwtAlgorithm.ES256,
            claims={
                "sub": "{api_key}",
                "iss": "cdp",
                "aud": "cdp",
                "uri": "{method} {host}{path}",
            },
            lifetime_seconds=120,
            jwt_headers={"kid": "{api_key}", "nonce": "{rand_hex}"},
            auth_header_name="Authorization",
            auth_header_template="Bearer {jwt}",
        )
        assert spec.algorithm == JwtAlgorithm.ES256
        assert spec.lifetime_seconds == 120
        assert spec.auth_header_name == "Authorization"

    def test_isinstance_discrimination(self) -> None:
        km = KeyMaterialSpec(encoding=KeyEncoding.PEM_EC)
        spec = JwtSigningSpec(
            key_material=km,
            algorithm=JwtAlgorithm.ES256,
            claims={"sub": "{api_key}"},
            lifetime_seconds=120,
            jwt_headers={},
            auth_header_name="Authorization",
            auth_header_template="Bearer {jwt}",
        )
        assert isinstance(spec, JwtSigningSpec)
        assert not isinstance(spec, HmacSigningSpec)
        assert not isinstance(spec, BearerTokenSpec)

    def test_frozen_immutable(self) -> None:
        km = KeyMaterialSpec(encoding=KeyEncoding.PEM_RSA)
        spec = JwtSigningSpec(
            key_material=km,
            algorithm=JwtAlgorithm.RS256,
            claims={"sub": "{api_key}"},
            lifetime_seconds=60,
            jwt_headers={},
            auth_header_name="Authorization",
            auth_header_template="Bearer {jwt}",
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.algorithm = JwtAlgorithm.ES256  # type: ignore[misc]

    def test_rs256_algorithm(self) -> None:
        km = KeyMaterialSpec(encoding=KeyEncoding.PEM_RSA)
        spec = JwtSigningSpec(
            key_material=km,
            algorithm=JwtAlgorithm.RS256,
            claims={},
            lifetime_seconds=300,
            jwt_headers={},
            auth_header_name="Authorization",
            auth_header_template="Bearer {jwt}",
        )
        assert spec.algorithm == JwtAlgorithm.RS256


# ---------------------------------------------------------------------------
# BearerTokenSpec
# ---------------------------------------------------------------------------


class TestBearerTokenSpec:
    def test_construct_valid(self) -> None:
        spec = BearerTokenSpec(
            token_endpoint="auth_token",
            token_request_template={"api_key": "{api_key}", "api_secret": "{secret}"},
            token_response_path="data.token",
            token_ttl_seconds=3600,
            auth_header_name="Authorization",
            auth_header_template="Bearer {token}",
        )
        assert spec.token_endpoint == "auth_token"
        assert spec.token_ttl_seconds == 3600
        assert spec.token_response_path == "data.token"

    def test_isinstance_discrimination(self) -> None:
        spec = BearerTokenSpec(
            token_endpoint="auth_token",
            token_request_template={},
            token_response_path="result.access_token",
            token_ttl_seconds=7200,
            auth_header_name="Authorization",
            auth_header_template="Bearer {token}",
        )
        assert isinstance(spec, BearerTokenSpec)
        assert not isinstance(spec, HmacSigningSpec)
        assert not isinstance(spec, JwtSigningSpec)

    def test_frozen_immutable(self) -> None:
        spec = BearerTokenSpec(
            token_endpoint="auth_token",
            token_request_template={},
            token_response_path="data.token",
            token_ttl_seconds=3600,
            auth_header_name="Authorization",
            auth_header_template="Bearer {token}",
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.token_endpoint = "other"  # type: ignore[misc]

    def test_ndax_pattern(self) -> None:
        spec = BearerTokenSpec(
            token_endpoint="ndax_login",
            token_request_template={
                "UserName": "{api_key}",
                "Password": "{secret}",
            },
            token_response_path="result.SessionToken",
            token_ttl_seconds=1800,
            auth_header_name="Authorization",
            auth_header_template="Bearer {token}",
        )
        assert spec.token_response_path == "result.SessionToken"


# ---------------------------------------------------------------------------
# Enum completeness checks
# ---------------------------------------------------------------------------


class TestEnumCompleteness:
    def test_key_encoding_values(self) -> None:
        names = {e.name for e in KeyEncoding}
        assert names == {"RAW_STR", "BASE64", "HEX", "PEM_EC", "PEM_RSA"}

    def test_timestamp_unit_values(self) -> None:
        names = {e.name for e in TimestampUnit}
        assert names == {"SECONDS", "MILLISECONDS", "NANOSECONDS"}

    def test_timestamp_format_values(self) -> None:
        names = {e.name for e in TimestampFormat}
        assert names == {"INTEGER", "ISO8601", "ISO8601_Z"}

    def test_nonce_source_values(self) -> None:
        names = {e.name for e in NonceSource}
        assert names == {"TIMESTAMP", "COUNTER", "UUID", "NONE"}

    def test_nonce_placement_values(self) -> None:
        names = {e.name for e in NoncePlacement}
        assert names == {"HEADER", "BODY_FIELD", "QS_FIELD", "SIG_ONLY", "NONE"}

    def test_body_format_values(self) -> None:
        names = {e.name for e in BodyFormat}
        assert names == {"JSON", "FORM_URLENCODED", "NONE"}

    def test_sig_algorithm_values(self) -> None:
        names = {e.name for e in SigAlgorithm}
        assert names == {"HMAC_SHA256", "HMAC_SHA512"}

    def test_sig_encoding_values(self) -> None:
        names = {e.name for e in SigEncoding}
        assert names == {"HEX", "BASE64"}

    def test_hash_algorithm_values(self) -> None:
        names = {e.name for e in HashAlgorithm}
        assert names == {"SHA256", "SHA512"}

    def test_jwt_algorithm_values(self) -> None:
        names = {e.name for e in JwtAlgorithm}
        assert names == {"ES256", "RS256"}

    def test_token_inject_strategy_defined(self) -> None:
        # TokenInjectStrategy must be importable; exact values not mandated by §6.5
        assert TokenInjectStrategy is not None


# ---------------------------------------------------------------------------
# SigningSpec union type annotation (smoke)
# ---------------------------------------------------------------------------


def test_signing_spec_union_import() -> None:
    from market_connector.auth.spec import SigningSpec

    km = KeyMaterialSpec(encoding=KeyEncoding.RAW_STR)
    ts = TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.ISO8601)
    nonce = NonceSpec(
        source=NonceSource.NONE, monotonic=False, placement=NoncePlacement.NONE, field_name=None
    )
    recipe = SignatureRecipe(
        template="{ts}{method}{path}",
        body_format=BodyFormat.NONE,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.BASE64,
    )
    output = AuthOutputSpec(headers={}, body_inject=None, qs_inject=None)
    spec: SigningSpec = HmacSigningSpec(
        key_material=km, timestamp=ts, nonce=nonce, recipe=recipe, output=output
    )
    assert isinstance(spec, HmacSigningSpec)
