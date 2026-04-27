"""Declarative specs for Coinbase Advanced Trade API authentication and rate limits.

Stage 3 Task 14: replaces inline auth recipes in auth.py with typed SigningSpec
dataclasses.  The constants here are consumed by DeclarativeRestSigner (Task 15)
and FlatRateLimit (already implemented in rate_limits/flat.py).
"""

from __future__ import annotations

from market_connector.auth.spec import (
    AuthOutputSpec,
    BodyFormat,
    HmacSigningSpec,
    JwtAlgorithm,
    JwtSigningSpec,
    KeyEncoding,
    KeyMaterialSpec,
    NonceSpec,
    NonceSource,
    NoncePlacement,
    SigAlgorithm,
    SigEncoding,
    SignatureRecipe,
    TimestampFormat,
    TimestampSpec,
    TimestampUnit,
)
from market_connector.rate_limits.flat import FlatRateLimitSpec
from market_connector.rate_limits.pool import PoolSpec
from market_connector.ws_models.decoder import JsonEnvelopeDecoder, WsMessageKind

# ---------------------------------------------------------------------------
# HMAC signing spec (legacy API keys)
# ---------------------------------------------------------------------------

COINBASE_HMAC_SPEC: HmacSigningSpec = HmacSigningSpec(
    key_material=KeyMaterialSpec(encoding=KeyEncoding.RAW_STR),
    timestamp=TimestampSpec(unit=TimestampUnit.SECONDS, format=TimestampFormat.INTEGER),
    # Coinbase HMAC does not use a separate nonce — timestamp serves as replay guard.
    nonce=NonceSpec(
        source=NonceSource.NONE,
        monotonic=False,
        placement=NoncePlacement.NONE,
        field_name=None,
    ),
    recipe=SignatureRecipe(
        # auth.py: _hmac_sign(secret, ts + method + path + body)
        template="{ts}{method}{path}{body}",
        body_format=BodyFormat.JSON,
        body_hash=None,
        algorithm=SigAlgorithm.HMAC_SHA256,
        output_encoding=SigEncoding.HEX,
    ),
    output=AuthOutputSpec(
        headers={
            "CB-ACCESS-KEY": "{api_key}",
            "CB-ACCESS-SIGN": "{sig}",
            "CB-ACCESS-TIMESTAMP": "{ts}",
            "Content-Type": "application/json",
            "User-Agent": "hb-market-connector/0.2.0",
        },
        body_inject=None,
        qs_inject=None,
    ),
)

# ---------------------------------------------------------------------------
# JWT signing spec (CDP API keys — EC / ES256)
# ---------------------------------------------------------------------------

COINBASE_JWT_SPEC: JwtSigningSpec = JwtSigningSpec(
    key_material=KeyMaterialSpec(encoding=KeyEncoding.PEM_EC),
    algorithm=JwtAlgorithm.ES256,
    claims={
        "sub": "{api_key}",
        "iss": "cdp",
        "aud": ["cdp"],
        "nbf": "{ts}",
        # exp is intentionally omitted: the JWT signer derives exp = nbf + lifetime_seconds
        # and injects it directly — no substitution variable needed here.
        "uri": "{method} {host}{path}",  # omitted for WS calls (signer checks context)
    },
    lifetime_seconds=120,
    jwt_headers={
        "kid": "{api_key}",
        "nonce": "{rand_hex}",
    },
    auth_header_name="Authorization",
    auth_header_template="Bearer {jwt}",
)

# ---------------------------------------------------------------------------
# Rate-limit spec
# ---------------------------------------------------------------------------
#
# endpoints.py declares:
#   Public  (server_time, products, product_book, candles):  limit=10, window=1.0 s
#   Private (accounts, place_order, cancel_orders, ...):     limit=30, window=1.0 s
#
# Two token-bucket pools: capacity = limit, refill_rate = limit/window tokens/s.

_PUBLIC_POOL = PoolSpec(name="public", capacity=10, refill_rate=10.0)
_PRIVATE_POOL = PoolSpec(name="private", capacity=30, refill_rate=30.0)

COINBASE_RATE_LIMIT_SPEC: FlatRateLimitSpec = FlatRateLimitSpec(
    pools={
        "public": _PUBLIC_POOL,
        "private": _PRIVATE_POOL,
    },
    endpoint_pools={
        # Public endpoints
        "server_time": [("public", 1)],
        "products": [("public", 1)],
        "product_book": [("public", 1)],
        "candles": [("public", 1)],
        # Private endpoints
        "accounts": [("private", 1)],
        "place_order": [("private", 1)],
        "cancel_orders": [("private", 1)],
        "list_orders": [("private", 1)],
        "order_status": [("private", 1)],
        "order_fills": [("private", 1)],
        "fee_summary": [("private", 1)],
    },
)

# ---------------------------------------------------------------------------
# WebSocket shape decoder spec
# ---------------------------------------------------------------------------
#
# Coinbase WS envelope: {"channel": "...", "timestamp": "...", "events": [...]}
# Channel values: "l2_data", "market_trades", "user", "heartbeats", "subscriptions"

COINBASE_WS_SHAPE_DECODER_SPEC: dict = {
    "channel_field": "channel",
    "pair_field": None,
    "payload_field": "events",
    "kind_dispatch": {
        "heartbeats": WsMessageKind.HEARTBEAT,
        "subscriptions": WsMessageKind.SUBSCRIBE_ACK,
        "l2_data": WsMessageKind.DATA,
        "market_trades": WsMessageKind.DATA,
        "user": WsMessageKind.DATA,
    },
    "error_field": None,
}

__all__ = [
    "COINBASE_HMAC_SPEC",
    "COINBASE_JWT_SPEC",
    "COINBASE_RATE_LIMIT_SPEC",
    "COINBASE_WS_SHAPE_DECODER_SPEC",
]
