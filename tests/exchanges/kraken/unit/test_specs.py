"""Unit tests for Kraken declarative specs.

Covers: HMAC spec structure, rate-limit tier/pool routing, symbol mapper
round-trips, and WS auth model types.
"""

from __future__ import annotations

import pytest

from market_connector.auth.spec import (
    BodyFormat,
    KeyEncoding,
    NoncePlacement,
    NonceSource,
    SigAlgorithm,
    SigEncoding,
    TimestampUnit,
)
from market_connector.exchanges.kraken.specs import (
    KRAKEN_HMAC_SPEC,
    KRAKEN_PRIVATE_WS_AUTH,
    KRAKEN_PUBLIC_WS_AUTH,
    KRAKEN_RATE_LIMIT_SPEC,
    KRAKEN_SYMBOL_MAPPER,
    KRAKEN_WS_DECODER,
    KrakenWsDecoder,
)
from market_connector.ws_models.auth_models import (
    PassThroughAuth,
    TokenFetchAuth,
    TokenInjectStrategy,
)
from market_connector.ws_models.decoder import WsMessageKind


class TestKrakenHmacSpec:
    def test_key_encoding_is_base64(self) -> None:
        assert KRAKEN_HMAC_SPEC.key_material.encoding == KeyEncoding.BASE64

    def test_timestamp_unit_is_milliseconds(self) -> None:
        assert KRAKEN_HMAC_SPEC.timestamp.unit == TimestampUnit.MILLISECONDS

    def test_nonce_source_is_counter(self) -> None:
        assert KRAKEN_HMAC_SPEC.nonce.source == NonceSource.COUNTER

    def test_nonce_is_monotonic(self) -> None:
        assert KRAKEN_HMAC_SPEC.nonce.monotonic is True

    def test_nonce_placement_is_body_field(self) -> None:
        assert KRAKEN_HMAC_SPEC.nonce.placement == NoncePlacement.BODY_FIELD

    def test_nonce_field_name_is_nonce(self) -> None:
        assert KRAKEN_HMAC_SPEC.nonce.field_name == "nonce"

    def test_recipe_template_is_bytes_concat(self) -> None:
        # Framework detects {path_bytes}{inner_hash} pattern at declarative.py:542
        assert KRAKEN_HMAC_SPEC.recipe.template == "{path_bytes}{inner_hash}"

    def test_recipe_body_format_is_form_urlencoded(self) -> None:
        assert KRAKEN_HMAC_SPEC.recipe.body_format == BodyFormat.FORM_URLENCODED

    def test_recipe_algorithm_is_hmac_sha512(self) -> None:
        assert KRAKEN_HMAC_SPEC.recipe.algorithm == SigAlgorithm.HMAC_SHA512

    def test_recipe_output_encoding_is_base64(self) -> None:
        assert KRAKEN_HMAC_SPEC.recipe.output_encoding == SigEncoding.BASE64

    def test_body_hash_is_sha256_over_nonce_body(self) -> None:
        assert KRAKEN_HMAC_SPEC.recipe.body_hash is not None
        assert "{nonce}" in KRAKEN_HMAC_SPEC.recipe.body_hash.input_template
        assert "{body}" in KRAKEN_HMAC_SPEC.recipe.body_hash.input_template

    def test_output_headers_contain_api_key(self) -> None:
        assert "API-Key" in KRAKEN_HMAC_SPEC.output.headers

    def test_output_headers_contain_api_sign(self) -> None:
        assert "API-Sign" in KRAKEN_HMAC_SPEC.output.headers

    def test_output_api_key_template(self) -> None:
        assert KRAKEN_HMAC_SPEC.output.headers["API-Key"] == "{api_key}"

    def test_output_api_sign_template(self) -> None:
        assert KRAKEN_HMAC_SPEC.output.headers["API-Sign"] == "{sig}"


class TestKrakenRateLimitSpec:
    def test_public_pool_exists(self) -> None:
        assert "public" in KRAKEN_RATE_LIMIT_SPEC.public_pools

    def test_public_pool_capacity_is_1(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.public_pools["public"]
        assert pool.capacity == 1

    def test_all_three_tiers_exist(self) -> None:
        assert "STARTER" in KRAKEN_RATE_LIMIT_SPEC.tiers
        assert "INTERMEDIATE" in KRAKEN_RATE_LIMIT_SPEC.tiers
        assert "PRO" in KRAKEN_RATE_LIMIT_SPEC.tiers

    @pytest.mark.parametrize("tier", ["STARTER", "INTERMEDIATE", "PRO"])
    def test_tier_has_private_and_matching_pools(self, tier: str) -> None:
        profile = KRAKEN_RATE_LIMIT_SPEC.tiers[tier]
        assert "private" in profile.pools
        assert "matching" in profile.pools

    def test_starter_private_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["STARTER"].pools["private"]
        assert pool.capacity == 15

    def test_starter_matching_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["STARTER"].pools["matching"]
        assert pool.capacity == 60

    def test_intermediate_private_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["INTERMEDIATE"].pools["private"]
        assert pool.capacity == 20

    def test_intermediate_matching_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["INTERMEDIATE"].pools["matching"]
        assert pool.capacity == 125

    def test_pro_private_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["PRO"].pools["private"]
        assert pool.capacity == 20

    def test_pro_matching_pool_capacity(self) -> None:
        pool = KRAKEN_RATE_LIMIT_SPEC.tiers["PRO"].pools["matching"]
        assert pool.capacity == 180

    def test_endpoint_pools_covers_public_endpoints(self) -> None:
        expected = {"server_time", "assets", "asset_pairs", "ticker"}
        assert expected <= set(KRAKEN_RATE_LIMIT_SPEC.endpoint_pools.keys())

    def test_endpoint_pools_covers_private_endpoints(self) -> None:
        expected = {"balance", "open_orders", "add_order", "cancel_order"}
        assert expected <= set(KRAKEN_RATE_LIMIT_SPEC.endpoint_pools.keys())

    def test_public_endpoint_routes_to_public_pool(self) -> None:
        charges = KRAKEN_RATE_LIMIT_SPEC.endpoint_pools["server_time"]
        assert charges == [("public", 1)]

    def test_private_endpoint_routes_to_private_pool(self) -> None:
        charges = KRAKEN_RATE_LIMIT_SPEC.endpoint_pools["balance"]
        pool_names = [name for name, _ in charges]
        assert "private" in pool_names

    def test_matching_endpoint_routes_to_both_pools(self) -> None:
        charges = KRAKEN_RATE_LIMIT_SPEC.endpoint_pools["add_order"]
        pool_names = [name for name, _ in charges]
        assert "private" in pool_names
        assert "matching" in pool_names


class TestKrakenSymbolMapper:
    def test_from_exchange_asset_xxbt_to_btc(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("XXBT") == "BTC"

    def test_from_exchange_asset_zusd_to_usd(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("ZUSD") == "USD"

    def test_from_exchange_asset_xeth_to_eth(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("XETH") == "ETH"

    def test_from_exchange_asset_zeur_to_eur(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.from_exchange_asset("ZEUR") == "EUR"

    def test_to_exchange_asset_btc_to_xbt(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.to_exchange_asset("BTC") == "XBT"

    def test_to_exchange_asset_usd_to_zusd(self) -> None:
        assert KRAKEN_SYMBOL_MAPPER.to_exchange_asset("USD") == "ZUSD"

    def test_from_exchange_pair_xxbtzusd(self) -> None:
        # XXBTZUSD → BTC-USD
        result = KRAKEN_SYMBOL_MAPPER.from_exchange_pair("XXBTZUSD")
        assert result == "BTC-USD"

    def test_from_exchange_pair_xethzusd(self) -> None:
        # XETHZUSD → ETH-USD
        result = KRAKEN_SYMBOL_MAPPER.from_exchange_pair("XETHZUSD")
        assert result == "ETH-USD"

    def test_from_exchange_pair_xethzeur(self) -> None:
        # XETHZEUR → ETH-EUR
        result = KRAKEN_SYMBOL_MAPPER.from_exchange_pair("XETHZEUR")
        assert result == "ETH-EUR"

    def test_to_exchange_pair_btc_usd(self) -> None:
        # BTC-USD → XBTZUSD (preferred short form)
        result = KRAKEN_SYMBOL_MAPPER.to_exchange_pair("BTC-USD")
        assert result == "XBTZUSD"

    def test_to_exchange_pair_eth_usd(self) -> None:
        # ETH-USD → XETHZUSD
        result = KRAKEN_SYMBOL_MAPPER.to_exchange_pair("ETH-USD")
        assert result == "XETHZUSD"

    def test_to_exchange_pair_eth_eur(self) -> None:
        # ETH-EUR → XETHZEUR
        result = KRAKEN_SYMBOL_MAPPER.to_exchange_pair("ETH-EUR")
        assert result == "XETHZEUR"


class TestKrakenWsAuth:
    def test_public_ws_auth_is_passthrough(self) -> None:
        assert isinstance(KRAKEN_PUBLIC_WS_AUTH, PassThroughAuth)

    def test_private_ws_auth_is_token_fetch(self) -> None:
        assert isinstance(KRAKEN_PRIVATE_WS_AUTH, TokenFetchAuth)

    def test_private_ws_auth_token_endpoint(self) -> None:
        assert KRAKEN_PRIVATE_WS_AUTH.token_endpoint == "/0/private/GetWebSocketsToken"

    def test_private_ws_auth_token_response_path(self) -> None:
        assert KRAKEN_PRIVATE_WS_AUTH.token_response_path == "result.token"

    def test_private_ws_auth_ttl(self) -> None:
        assert KRAKEN_PRIVATE_WS_AUTH.token_ttl_seconds == 900

    def test_private_ws_auth_inject_strategy(self) -> None:
        assert KRAKEN_PRIVATE_WS_AUTH.inject_strategy == TokenInjectStrategy.SUBSCRIBE_PAYLOAD


class TestKrakenWsDecoder:
    def test_singleton_is_instance(self) -> None:
        assert isinstance(KRAKEN_WS_DECODER, KrakenWsDecoder)

    def test_heartbeat_dict_returns_heartbeat_kind(self) -> None:
        msg = KRAKEN_WS_DECODER.decode({"event": "heartbeat"})
        assert msg.kind == WsMessageKind.HEARTBEAT

    def test_system_status_dict_returns_unknown_kind(self) -> None:
        msg = KRAKEN_WS_DECODER.decode({"event": "systemStatus", "status": "online"})
        assert msg.kind == WsMessageKind.UNKNOWN

    def test_subscription_status_dict_returns_subscribe_ack(self) -> None:
        msg = KRAKEN_WS_DECODER.decode(
            {"event": "subscriptionStatus", "status": "subscribed", "channelName": "trade"}
        )
        assert msg.kind == WsMessageKind.SUBSCRIBE_ACK

    def test_array_frame_returns_data_kind(self) -> None:
        # Kraken data frame: [payload, sequence, channel, pair]
        raw_frame: list = [
            [{"price": "50000.0", "volume": "1.0"}],
            1234,
            "trade",
            "XBT/USD",
        ]
        msg = KRAKEN_WS_DECODER.decode(raw_frame)
        assert msg.kind == WsMessageKind.DATA

    def test_array_frame_extracts_channel(self) -> None:
        raw_frame: list = [
            [{"price": "50000.0"}],
            1234,
            "trade",
            "XBT/USD",
        ]
        msg = KRAKEN_WS_DECODER.decode(raw_frame)
        assert msg.channel == "trade"

    def test_array_frame_extracts_pair(self) -> None:
        raw_frame: list = [
            [{"price": "50000.0"}],
            1234,
            "trade",
            "XBT/USD",
        ]
        msg = KRAKEN_WS_DECODER.decode(raw_frame)
        assert msg.pair == "XBT/USD"
