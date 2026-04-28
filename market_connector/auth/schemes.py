"""Pre-built HmacSigningSpec constants for common CEX authentication families.

Each constant is a fully-constructed, frozen HmacSigningSpec instance covering
one CEX signing family from the §6.8 connector survey.  Exchanges instantiate
a scheme by calling ``dataclasses.replace`` to override the header / qs-inject
field names in ``AuthOutputSpec`` — the rest of the spec is shared.

Key material and timestamp sub-specs use the most common defaults for each
family; exchanges with non-standard credentials (e.g. base64 secret, derived
passphrase) apply additional ``replace`` calls or subclass ``KeyMaterialSpec``.

Module-level constants (4 families):
  - HMAC_QS_SORTED_TS          — Binance family (7 connectors)
  - HMAC_TS_METHOD_PATH_BODY_HEX    — OKX-family HEX (15 connectors)
  - HMAC_TS_METHOD_PATH_BODY_BASE64 — OKX/Bitget BASE64 (same template)
  - HMAC_GATE_IO_MULTILINE          — Gate.io SHA-512 multiline (2 connectors)
"""

from __future__ import annotations

from market_connector.auth.spec import (
    AuthOutputSpec,
    BodyFormat,
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
# Shared sub-spec defaults
# ---------------------------------------------------------------------------

_KEY_RAW_STR = KeyMaterialSpec(
    encoding=KeyEncoding.RAW_STR,
    derived_credentials=(),
)

_TS_SECONDS_INT = TimestampSpec(
    unit=TimestampUnit.SECONDS,
    format=TimestampFormat.INTEGER,
)

_TS_MILLISECONDS_INT = TimestampSpec(
    unit=TimestampUnit.MILLISECONDS,
    format=TimestampFormat.INTEGER,
)

_NONCE_NONE = NonceSpec(
    source=NonceSource.NONE,
    monotonic=False,
    placement=NoncePlacement.NONE,
    field_name=None,
)

# ---------------------------------------------------------------------------
# HMAC_QS_SORTED_TS — Binance family
#
# Spec §6.8: recipe.template="{qs_sorted}", output.qs_inject
# Covers: binance, binance_perpetual, mexc, bitrue, hashkey,
#         hashkey_perpetual, bing_x  (7 connectors).
#
# The signature is appended to the query-string; exchanges override the
# qs_inject key name with dataclasses.replace.
# ---------------------------------------------------------------------------

HMAC_QS_SORTED_TS: HmacSigningSpec = HmacSigningSpec(
    key_material=_KEY_RAW_STR,
    timestamp=_TS_MILLISECONDS_INT,
    nonce=_NONCE_NONE,
    recipe=SignatureRecipe(
        template="{qs_sorted}",
        body_format=BodyFormat.NONE,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.HEX,
    ),
    output=AuthOutputSpec(
        headers={},
        body_inject=None,
        qs_inject={"signature": "{sig}"},
    ),
)

# ---------------------------------------------------------------------------
# HMAC_TS_METHOD_PATH_BODY_HEX — OKX-family, hex output
#
# Spec §6.8: recipe.template="{ts}{method}{path}{body}", output.headers only
# Covers: ~15 connectors (OKX, Bybit, and similar).
# Output encoding: HEX.
# ---------------------------------------------------------------------------

_TS_METHOD_PATH_BODY_TEMPLATE = "{ts}{method}{path}{body}"

HMAC_TS_METHOD_PATH_BODY_HEX: HmacSigningSpec = HmacSigningSpec(
    key_material=_KEY_RAW_STR,
    timestamp=_TS_SECONDS_INT,
    nonce=_NONCE_NONE,
    recipe=SignatureRecipe(
        template=_TS_METHOD_PATH_BODY_TEMPLATE,
        body_format=BodyFormat.JSON,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.HEX,
    ),
    output=AuthOutputSpec(
        headers={"X-Signature": "{sig}"},
        body_inject=None,
        qs_inject=None,
    ),
)

# ---------------------------------------------------------------------------
# HMAC_TS_METHOD_PATH_BODY_BASE64 — OKX / Bitget base64 variant
#
# Same template as the HEX variant; only output_encoding differs.
# Covers: OKX REST, Bitget, and similar exchanges that prefer base64.
# ---------------------------------------------------------------------------

HMAC_TS_METHOD_PATH_BODY_BASE64: HmacSigningSpec = HmacSigningSpec(
    key_material=_KEY_RAW_STR,
    timestamp=_TS_SECONDS_INT,
    nonce=_NONCE_NONE,
    recipe=SignatureRecipe(
        template=_TS_METHOD_PATH_BODY_TEMPLATE,
        body_format=BodyFormat.JSON,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.BASE64,
    ),
    output=AuthOutputSpec(
        headers={"X-Signature": "{sig}"},
        body_inject=None,
        qs_inject=None,
    ),
)

# ---------------------------------------------------------------------------
# HMAC_GATE_IO_MULTILINE — Gate.io SHA-512 newline-separated template
#
# Spec §6.8: algorithm=HMAC_SHA512 + multi-line template.
# Gate.io signs: METHOD\nURL_PATH\nQUERY_STRING\nHEX(SHA512(body))\nTIMESTAMP
# Covers: gate_io, gate_io_perpetual (2 connectors).
#
# Note: Gate.io uses a body-hash inner stage in practice; the SHA-512 of the
# body is produced by an outer hash, not BodyHashSpec (which is for Kraken's
# two-stage HMAC-over-hash).  The template encodes this inline.
# ---------------------------------------------------------------------------

HMAC_GATE_IO_MULTILINE: HmacSigningSpec = HmacSigningSpec(
    key_material=_KEY_RAW_STR,
    timestamp=_TS_SECONDS_INT,
    nonce=_NONCE_NONE,
    recipe=SignatureRecipe(
        template="{method}\n{path}\n{qs}\n{body}\n{ts}",
        body_format=BodyFormat.NONE,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA512,
        output_encoding=SigEncoding.HEX,
    ),
    output=AuthOutputSpec(
        headers={"KEY": "{api_key}", "SIGN": "{sig}", "Timestamp": "{ts}"},
        body_inject=None,
        qs_inject=None,
    ),
)
