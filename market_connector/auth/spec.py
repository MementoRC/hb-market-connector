"""Declarative signing specification dataclasses (spec §6.3–6.5).

Provides the discriminated union::

    SigningSpec = HmacSigningSpec | JwtSigningSpec | BearerTokenSpec

DeclarativeRestSigner (Task 4) dispatches on isinstance to select the
correct signing strategy. All dataclasses are frozen (immutable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class KeyEncoding(Enum):
    """How the raw secret bytes are encoded in the credential store."""

    RAW_STR = "RAW_STR"
    BASE64 = "BASE64"
    HEX = "HEX"
    PEM_EC = "PEM_EC"
    PEM_RSA = "PEM_RSA"


class TimestampUnit(Enum):
    """Granularity of the timestamp injected into signing templates."""

    SECONDS = "SECONDS"
    MILLISECONDS = "MILLISECONDS"
    NANOSECONDS = "NANOSECONDS"


class TimestampFormat(Enum):
    """Serialisation format for the timestamp value."""

    INTEGER = "INTEGER"
    ISO8601 = "ISO8601"
    ISO8601_Z = "ISO8601_Z"


class NonceSource(Enum):
    """Source of nonce entropy."""

    TIMESTAMP = "TIMESTAMP"
    COUNTER = "COUNTER"
    UUID = "UUID"
    NONE = "NONE"


class NoncePlacement(Enum):
    """Where the nonce is injected into the outbound request."""

    HEADER = "HEADER"
    BODY_FIELD = "BODY_FIELD"
    QS_FIELD = "QS_FIELD"
    SIG_ONLY = "SIG_ONLY"  # fed into signing template but not sent as a field
    NONE = "NONE"


class BodyFormat(Enum):
    """How the request body is serialised before signing."""

    JSON = "JSON"
    FORM_URLENCODED = "FORM_URLENCODED"
    NONE = "NONE"


class SigAlgorithm(Enum):
    """HMAC signing algorithm."""

    HMAC_SHA256 = "HMAC_SHA256"
    HMAC_SHA512 = "HMAC_SHA512"


class SigEncoding(Enum):
    """Output encoding for the computed signature."""

    HEX = "HEX"
    BASE64 = "BASE64"


class HashAlgorithm(Enum):
    """One-way hash algorithm used in BodyHashSpec (separate from SigAlgorithm)."""

    SHA256 = "SHA256"
    SHA512 = "SHA512"


class JwtAlgorithm(Enum):
    """JWT signing algorithm."""

    ES256 = "ES256"
    RS256 = "RS256"


# ---------------------------------------------------------------------------
# HMAC sub-specs
# ---------------------------------------------------------------------------

# DerivationFn is intentionally kept as an opaque string label in this spec
# layer.  Task 4 (DeclarativeRestSigner) resolves labels to callables.
_DerivationFn = str


@dataclass(frozen=True)
class KeyMaterialSpec:
    """Describes how to decode the raw secret credential."""

    encoding: KeyEncoding
    derived_credentials: tuple[tuple[str, _DerivationFn], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TimestampSpec:
    """Timestamp unit and serialisation format injected into signing templates."""

    unit: TimestampUnit
    format: TimestampFormat


_PLACEMENT_REQUIRES_FIELD_NAME: frozenset[NoncePlacement] = frozenset(
    {NoncePlacement.HEADER, NoncePlacement.BODY_FIELD, NoncePlacement.QS_FIELD}
)


@dataclass(frozen=True)
class NonceSpec:
    """Nonce source, monotonicity flag, and injection placement.

    Cross-field constraint: ``field_name`` is required when placement is
    HEADER, BODY_FIELD, or QS_FIELD (i.e. any placement that injects a named
    field into the request).
    """

    source: NonceSource
    monotonic: bool
    placement: NoncePlacement
    field_name: str | None

    def __post_init__(self) -> None:
        if self.placement in _PLACEMENT_REQUIRES_FIELD_NAME and self.field_name is None:
            raise ValueError(
                f"field_name is required when placement is {self.placement.name}; got None"
            )


@dataclass(frozen=True)
class BodyHashSpec:
    """Inner hash stage used by two-stage signing recipes (e.g. Kraken)."""

    algorithm: HashAlgorithm
    input_template: str  # e.g. "{nonce}{body}"


@dataclass(frozen=True)
class SignatureRecipe:
    """Full description of how to compute the HMAC signature."""

    template: str  # e.g. "{ts}{method}{path}{body}"
    body_format: BodyFormat
    body_hash: BodyHashSpec | None
    algorithm: SigAlgorithm
    output_encoding: SigEncoding


@dataclass(frozen=True)
class AuthOutputSpec:
    """Where and how to inject the computed signature into the request."""

    headers: dict[str, str]  # header name → template, e.g. {"X-Signature": "{sig}"}
    body_inject: dict[str, str] | None  # body field → template
    qs_inject: dict[str, str] | None  # query-string param → template


# ---------------------------------------------------------------------------
# Top-level SigningSpec variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HmacSigningSpec:
    """Composable HMAC signing spec built from five orthogonal sub-specs.

    Covers ~28 of 36 surveyed CEX connectors via template parameterisation
    (spec §6.8).
    """

    key_material: KeyMaterialSpec
    timestamp: TimestampSpec
    nonce: NonceSpec
    recipe: SignatureRecipe
    output: AuthOutputSpec


@dataclass(frozen=True)
class JwtSigningSpec:
    """Monolithic JWT signing spec (Coinbase ES256 pattern, spec §6.4)."""

    key_material: KeyMaterialSpec  # PEM_EC for ES256 / PEM_RSA for RS256
    algorithm: JwtAlgorithm
    claims: dict[str, Any]  # {"sub": "{api_key}", "iss": "cdp", ...}
    lifetime_seconds: int
    jwt_headers: dict[str, str]  # {"kid": "{api_key}", "nonce": "{rand_hex}"}
    auth_header_name: str  # "Authorization"
    auth_header_template: str  # "Bearer {jwt}"


@dataclass(frozen=True)
class BearerTokenSpec:
    """Monolithic bearer-token spec (Architect / NDAX / Kucoin, spec §6.5)."""

    token_endpoint: str  # name in ENDPOINT_REGISTRY
    token_request_template: dict[str, str]  # {"api_key": "{api_key}", ...}
    token_response_path: str  # "data.token" or "result.access_token"
    token_ttl_seconds: int
    auth_header_name: str  # "Authorization"
    auth_header_template: str  # "Bearer {token}"


# ---------------------------------------------------------------------------
# Discriminated union type alias
# ---------------------------------------------------------------------------

SigningSpec = HmacSigningSpec | JwtSigningSpec | BearerTokenSpec
