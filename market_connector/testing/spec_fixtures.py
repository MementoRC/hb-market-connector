"""Reusable known-good spec fixtures for cross-test conformance validation.

Import these constants directly in test files — no pytest fixtures needed.
All specs are deterministic and require no I/O.
"""

from __future__ import annotations

from market_connector.auth.protocols import Request
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
from market_connector.rate_limits.flat import FlatRateLimitSpec
from market_connector.rate_limits.pool import PoolSpec
from market_connector.symbols.mapper import IdentityMapper, RuleBasedMapper
from market_connector.ws_models.decoder import (
    JsonEnvelopeDecoder,
    PositionalArrayDecoder,
    WsMessageKind,
)

# ---------------------------------------------------------------------------
# Auth / HMAC spec
# ---------------------------------------------------------------------------

KNOWN_HMAC_SPEC: HmacSigningSpec = HmacSigningSpec(
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
        headers={"X-Signature": "{sig}"},
        body_inject=None,
        qs_inject=None,
    ),
)

# Fixture request matched to KNOWN_HMAC_SPEC: GET /v1/ping, no body, no qs.
KNOWN_HMAC_REQUEST: Request = Request(
    method="GET",
    url="https://api.example.com/v1/ping",
    path="/v1/ping",
    headers={},
    body=None,
    qs_params={},
)

# ---------------------------------------------------------------------------
# WS shape decoders
# ---------------------------------------------------------------------------

# Coinbase-shaped JSON envelope: {"channel": "...", "product_id": "...", "data": {...}}
KNOWN_JSON_DECODER_SPEC: JsonEnvelopeDecoder = JsonEnvelopeDecoder(
    channel_field="channel",
    pair_field="product_id",
    payload_field="data",
    kind_dispatch={
        "heartbeats": WsMessageKind.HEARTBEAT,
        "subscriptions": WsMessageKind.SUBSCRIBE_ACK,
    },
    error_field=None,
)

# Kraken-shaped positional array: [payload, seq, pair, channel]
# indices: channel=-1, pair=-2, payload=0
KNOWN_ARRAY_DECODER_SPEC: PositionalArrayDecoder = PositionalArrayDecoder(
    channel_index=-1,
    pair_index=-2,
    payload_index=0,
    heartbeat_predicate=None,
    subscribe_ack_predicate=None,
)

# ---------------------------------------------------------------------------
# Symbol mappers
# ---------------------------------------------------------------------------

# Identity mapper: BTC-USD ↔ BTC-USD (dash separator, like Coinbase/OKX)
KNOWN_IDENTITY_MAPPER_SPEC: IdentityMapper = IdentityMapper(separator="-")

# Rule-based mapper: BTC-USDT ↔ BTCUSDT (no separator, like Binance)
KNOWN_RULE_MAPPER_SPEC: RuleBasedMapper = RuleBasedMapper(
    separator=None,
    known_quote_assets=("USDT", "USD", "BTC", "ETH", "BNB"),
)

# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

# Permissive flat rate-limit spec: large bucket so tests don't need to wait.
# capacity=1000, refill=1000/s → 1 token replenished per millisecond.
KNOWN_FLAT_RATE_LIMIT_SPEC: FlatRateLimitSpec = FlatRateLimitSpec(
    pools={
        "default": PoolSpec(name="default", capacity=1000, refill_rate=1000.0),
    },
    endpoint_pools={
        "test_endpoint": [("default", 1)],
    },
)
