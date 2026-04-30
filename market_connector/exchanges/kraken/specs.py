"""Declarative specs for Kraken REST API authentication, rate limits, symbol mapping,
and WebSocket message decoding.

All specs are consumed by DeclarativeRestSigner, TieredRateLimit, RuleBasedMapper,
and KrakenWsDecoder.  Stage 2 will wire these into conformance test suites.

Signing algorithm (two-stage Kraken HMAC-SHA512):
  1. inner_hash = SHA256(nonce + url_encoded_body)          # bytes
  2. sig_input  = path_bytes + inner_hash                   # bytes concat
  3. signature  = HMAC-SHA512(base64_decode(secret), sig_input)
  4. output     = base64_encode(signature)
  Headers: API-Key, API-Sign

Reference: hummingbot/connector/exchange/kraken/kraken_auth.py — _generate_auth_dict
"""

from __future__ import annotations

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
from market_connector.exchanges.kraken._aliases_generated import KRAKEN_ASSET_ALIASES
from market_connector.rate_limits.pool import PoolSpec
from market_connector.rate_limits.tiered import TieredRateLimitSpec, TierProfile
from market_connector.symbols.mapper import RuleBasedMapper
from market_connector.ws_models.auth_models import (
    PassThroughAuth,
    TokenFetchAuth,
    TokenInjectStrategy,
)
from market_connector.ws_models.decoder import (
    NormalizedWsMessage,
    PositionalArrayDecoder,
    WsMessageKind,
)

# ---------------------------------------------------------------------------
# HMAC signing spec (Kraken two-stage HMAC-SHA512)
# ---------------------------------------------------------------------------

KRAKEN_HMAC_SPEC: HmacSigningSpec = HmacSigningSpec(
    key_material=KeyMaterialSpec(encoding=KeyEncoding.BASE64),
    timestamp=TimestampSpec(unit=TimestampUnit.MILLISECONDS, format=TimestampFormat.INTEGER),
    nonce=NonceSpec(
        source=NonceSource.COUNTER,
        monotonic=True,
        placement=NoncePlacement.BODY_FIELD,
        field_name="nonce",
    ),
    recipe=SignatureRecipe(
        # Framework detects "{path_bytes}{inner_hash}" → bytes-concat mode (declarative.py:542)
        template="{path_bytes}{inner_hash}",
        body_format=BodyFormat.FORM_URLENCODED,
        body_hash=BodyHashSpec(
            algorithm=HashAlgorithm.SHA256,
            input_template="{nonce}{body}",  # SHA256(nonce + url_encoded_body)
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

# ---------------------------------------------------------------------------
# Rate-limit spec (three tiers: STARTER / INTERMEDIATE / PRO)
# ---------------------------------------------------------------------------
#
# Kraken rate-limit tiers (per https://support.kraken.com/hc/en-us/articles/360045239571):
#   Public pool: 1 rps shared
#   STARTER  private: 15 counter / 20 burst;  matching: 60/60
#   INTERMEDIATE private: 20/30;  matching: 125/140
#   PRO      private: 20/60;  matching: 180/225

_PUBLIC_POOL = PoolSpec(name="public", capacity=1, refill_rate=1.0)

_STARTER_PRIVATE_POOL = PoolSpec(name="private", capacity=15, refill_rate=15.0)
_STARTER_MATCHING_POOL = PoolSpec(name="matching", capacity=60, refill_rate=60.0)

_INTERMEDIATE_PRIVATE_POOL = PoolSpec(name="private", capacity=20, refill_rate=20.0)
_INTERMEDIATE_MATCHING_POOL = PoolSpec(name="matching", capacity=125, refill_rate=125.0)

_PRO_PRIVATE_POOL = PoolSpec(name="private", capacity=20, refill_rate=20.0)
_PRO_MATCHING_POOL = PoolSpec(name="matching", capacity=180, refill_rate=180.0)

KRAKEN_RATE_LIMIT_SPEC: TieredRateLimitSpec = TieredRateLimitSpec(
    public_pools={"public": _PUBLIC_POOL},
    tiers={
        "STARTER": TierProfile(
            name="STARTER",
            pools={
                "private": _STARTER_PRIVATE_POOL,
                "matching": _STARTER_MATCHING_POOL,
            },
        ),
        "INTERMEDIATE": TierProfile(
            name="INTERMEDIATE",
            pools={
                "private": _INTERMEDIATE_PRIVATE_POOL,
                "matching": _INTERMEDIATE_MATCHING_POOL,
            },
        ),
        "PRO": TierProfile(
            name="PRO",
            pools={
                "private": _PRO_PRIVATE_POOL,
                "matching": _PRO_MATCHING_POOL,
            },
        ),
    },
    # Endpoint → pool routing is defined in Stage 3 (endpoints.py).
    # Skeleton entries provided here for the public pool.
    endpoint_pools={
        "server_time": [("public", 1)],
        "assets": [("public", 1)],
        "asset_pairs": [("public", 1)],
        "ticker": [("public", 1)],
        "ohlc": [("public", 1)],
        "order_book": [("public", 1)],
        "recent_trades": [("public", 1)],
        "balance": [("private", 1)],
        "open_orders": [("private", 1)],
        "closed_orders": [("private", 1)],
        "add_order": [("private", 1), ("matching", 1)],
        "cancel_order": [("private", 1), ("matching", 1)],
        "get_ws_token": [("private", 1)],
    },
)

# ---------------------------------------------------------------------------
# Symbol mapper (Kraken REST — no separator, concatenated pairs)
# ---------------------------------------------------------------------------
#
# Kraken uses concatenated native asset codes (e.g. XXBTZUSD).
# _aliases_generated.py maps Kraken codes → Hummingbot canonical assets.
# The inverse (from_hb) is derived from the same table.

_aliases_from_hb: dict[str, str] = {v: k for k, v in KRAKEN_ASSET_ALIASES.items()}

# When multiple Kraken codes map to the same canonical asset (e.g. XBT and XXBT → BTC),
# prefer the shorter / more common form for the reverse mapping.
_PREFERRED_FROM_HB: dict[str, str] = {
    "BTC": "XBT",
    "DOGE": "XDG",
    "ETC": "XETC",
    "ETH": "XETH",
    "LTC": "XLTC",
    "MLN": "XMLN",
    "REP": "XREP",
    "XLM": "XXLM",
    "XMR": "XXMR",
    "XRP": "XXRP",
    "ZEC": "XZEC",
    "AUD": "ZAUD",
    "CAD": "ZCAD",
    "EUR": "ZEUR",
    "GBP": "ZGBP",
    "JPY": "ZJPY",
    "USD": "ZUSD",
}

# Known quote assets for no-separator split (longest-suffix wins)
_KNOWN_QUOTE_ASSETS: tuple[str, ...] = (
    "ZUSD",
    "ZEUR",
    "ZGBP",
    "ZCAD",
    "ZJPY",
    "ZAUD",
    "XXBT",
    "XETH",
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "JPY",
    "AUD",
    "BTC",
    "ETH",
)

KRAKEN_SYMBOL_MAPPER: RuleBasedMapper = RuleBasedMapper(
    separator=None,
    known_quote_assets=_KNOWN_QUOTE_ASSETS,
    asset_aliases_to_hb=KRAKEN_ASSET_ALIASES,
    asset_aliases_from_hb=_PREFERRED_FROM_HB,
)

# ---------------------------------------------------------------------------
# WebSocket auth specs
# ---------------------------------------------------------------------------

# Public WebSocket (wss://ws.kraken.com) — no authentication required.
KRAKEN_PUBLIC_WS_AUTH: PassThroughAuth = PassThroughAuth()

# Private WebSocket (wss://ws-auth.kraken.com) — token-based auth.
# Token fetched via /0/private/GetWebSocketsToken; injected into subscribe payloads.
# rest_client is injected at runtime by kraken_ws_auth_factory().
KRAKEN_PRIVATE_WS_AUTH: TokenFetchAuth = TokenFetchAuth(
    token_endpoint="/0/private/GetWebSocketsToken",
    token_response_path="result.token",
    token_ttl_seconds=900,
    inject_strategy=TokenInjectStrategy.SUBSCRIBE_PAYLOAD,
    rest_client=None,  # bound by factory at runtime
)

# ---------------------------------------------------------------------------
# WebSocket decoder
# ---------------------------------------------------------------------------


class KrakenWsDecoder:
    """WS decoder for Kraken v1 array-frame protocol.

    Kraken sends two distinct frame shapes:
    - Dict frames: control messages (heartbeat, systemStatus, subscriptionStatus)
    - Array frames: data frames [payload, sequence, channel, pair]

    Dict-form control frames are pre-classified here before delegating to the
    inner PositionalArrayDecoder (which handles array frames and subscriptionStatus
    acks, both of which it already supports per its docstring).
    """

    _HEARTBEAT_EVENT = "heartbeat"
    _SYSTEM_STATUS_EVENT = "systemStatus"
    _SUBSCRIPTION_STATUS_EVENT = "subscriptionStatus"

    def __init__(self) -> None:
        self._inner = PositionalArrayDecoder(
            channel_index=-2,
            pair_index=-1,
            payload_index=1,
            heartbeat_predicate=None,  # heartbeats handled above as dict frames
            subscribe_ack_predicate=lambda raw: (
                isinstance(raw, dict) and raw.get("event") == self._SUBSCRIPTION_STATUS_EVENT
            ),
        )

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:
        """Classify and decode a raw Kraken WS frame."""
        if isinstance(raw, dict):
            event = raw.get("event")
            if event == self._HEARTBEAT_EVENT:
                return NormalizedWsMessage(
                    kind=WsMessageKind.HEARTBEAT,
                    channel=None,
                    pair=None,
                    payload=raw,
                    error=None,
                )
            if event == self._SYSTEM_STATUS_EVENT:
                return NormalizedWsMessage(
                    kind=WsMessageKind.UNKNOWN,
                    channel=None,
                    pair=None,
                    payload=raw,
                    error=None,
                )
            # subscriptionStatus and unknown dicts → delegate to inner decoder
            return self._inner.decode(raw)

        # Array frames → delegate to inner decoder
        return self._inner.decode(raw)


# Singleton instance for use in Stage 2 conformance wiring
KRAKEN_WS_DECODER: KrakenWsDecoder = KrakenWsDecoder()

__all__ = [
    "KRAKEN_HMAC_SPEC",
    "KRAKEN_RATE_LIMIT_SPEC",
    "KRAKEN_SYMBOL_MAPPER",
    "KRAKEN_PUBLIC_WS_AUTH",
    "KRAKEN_PRIVATE_WS_AUTH",
    "KRAKEN_WS_DECODER",
    "KrakenWsDecoder",
]
