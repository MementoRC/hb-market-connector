"""Structural correctness tests for Coinbase signing/rate-limit/WS-decoder specs."""

from __future__ import annotations

import inspect

from market_connector.auth.spec import JwtAlgorithm, SigAlgorithm, SigEncoding
from market_connector.exchanges.coinbase.specs import (
    COINBASE_HMAC_SPEC,
    COINBASE_JWT_SPEC,
    COINBASE_RATE_LIMIT_SPEC,
    COINBASE_WS_SHAPE_DECODER_SPEC,
)
from market_connector.ws_models.decoder import JsonEnvelopeDecoder


class TestCoinbaseHmacSpec:
    def test_recipe_algorithm_is_hmac_sha256(self) -> None:
        assert COINBASE_HMAC_SPEC.recipe.algorithm == SigAlgorithm.HMAC_SHA256

    def test_recipe_output_encoding_is_hex(self) -> None:
        assert COINBASE_HMAC_SPEC.recipe.output_encoding == SigEncoding.HEX

    def test_recipe_template_matches_auth_recipe(self) -> None:
        # auth.py: ts + method + path + body
        assert COINBASE_HMAC_SPEC.recipe.template == "{ts}{method}{path}{body}"

    def test_output_headers_contain_required_keys(self) -> None:
        headers = COINBASE_HMAC_SPEC.output.headers
        assert "CB-ACCESS-KEY" in headers
        assert "CB-ACCESS-SIGN" in headers
        assert "CB-ACCESS-TIMESTAMP" in headers

    def test_output_headers_templates(self) -> None:
        headers = COINBASE_HMAC_SPEC.output.headers
        assert "{api_key}" in headers["CB-ACCESS-KEY"]
        assert "{sig}" in headers["CB-ACCESS-SIGN"]
        assert "{ts}" in headers["CB-ACCESS-TIMESTAMP"]


class TestCoinbaseJwtSpec:
    def test_algorithm_is_es256(self) -> None:
        assert COINBASE_JWT_SPEC.algorithm == JwtAlgorithm.ES256

    def test_lifetime_seconds(self) -> None:
        assert COINBASE_JWT_SPEC.lifetime_seconds == 120

    def test_claims_has_required_keys(self) -> None:
        # nbf and exp are computed by the JWT signer (nbf=ts, exp=nbf+lifetime_seconds),
        # not declared as format templates in the spec.
        claims = COINBASE_JWT_SPEC.claims
        for key in ("sub", "iss", "aud", "uri"):
            assert key in claims, f"Missing claim: {key}"

    def test_claims_iss_is_cdp(self) -> None:
        assert COINBASE_JWT_SPEC.claims["iss"] == "cdp"

    def test_claims_aud_references_cdp(self) -> None:
        aud = COINBASE_JWT_SPEC.claims["aud"]
        # May be a list or a template string referencing "cdp"
        if isinstance(aud, list):
            assert "cdp" in aud
        else:
            assert "cdp" in str(aud)

    def test_jwt_headers_has_kid_and_nonce(self) -> None:
        assert "kid" in COINBASE_JWT_SPEC.jwt_headers
        assert "nonce" in COINBASE_JWT_SPEC.jwt_headers

    def test_auth_header_name(self) -> None:
        assert COINBASE_JWT_SPEC.auth_header_name == "Authorization"

    def test_auth_header_template_contains_bearer(self) -> None:
        assert "Bearer" in COINBASE_JWT_SPEC.auth_header_template


class TestCoinbaseRateLimitSpec:
    def test_endpoint_pools_includes_server_time(self) -> None:
        assert "server_time" in COINBASE_RATE_LIMIT_SPEC.endpoint_pools

    def test_endpoint_pools_server_time_charges(self) -> None:
        charges = COINBASE_RATE_LIMIT_SPEC.endpoint_pools["server_time"]
        assert isinstance(charges, list)
        assert len(charges) >= 1
        pool_name, weight = charges[0]
        assert isinstance(pool_name, str)
        assert isinstance(weight, int)

    def test_endpoint_pools_covers_all_endpoints(self) -> None:
        expected = {
            "server_time", "products", "product_book", "candles",
            "accounts", "place_order", "cancel_orders", "list_orders",
            "order_status", "order_fills", "fee_summary",
        }
        actual = set(COINBASE_RATE_LIMIT_SPEC.endpoint_pools.keys())
        assert expected <= actual, f"Missing endpoints: {expected - actual}"

    def test_pools_exist_for_all_referenced_pool_names(self) -> None:
        pool_names = set(COINBASE_RATE_LIMIT_SPEC.pools.keys())
        for ep, charges in COINBASE_RATE_LIMIT_SPEC.endpoint_pools.items():
            for pool_name, _ in charges:
                assert pool_name in pool_names, (
                    f"Endpoint '{ep}' references unknown pool '{pool_name}'"
                )

    def test_public_pool_capacity(self) -> None:
        # Public endpoints: limit=10/window
        public_ep = COINBASE_RATE_LIMIT_SPEC.endpoint_pools["server_time"]
        pool_name = public_ep[0][0]
        pool = COINBASE_RATE_LIMIT_SPEC.pools[pool_name]
        assert pool.capacity == 10

    def test_private_pool_capacity(self) -> None:
        # Private endpoints: limit=30/window
        private_ep = COINBASE_RATE_LIMIT_SPEC.endpoint_pools["place_order"]
        pool_name = private_ep[0][0]
        pool = COINBASE_RATE_LIMIT_SPEC.pools[pool_name]
        assert pool.capacity == 30


class TestCoinbaseWsShapeDecoderSpec:
    def test_is_dict(self) -> None:
        assert isinstance(COINBASE_WS_SHAPE_DECODER_SPEC, dict)

    def test_keys_match_json_envelope_decoder_parameters(self) -> None:
        sig = inspect.signature(JsonEnvelopeDecoder.__init__)
        valid_params = set(sig.parameters.keys()) - {"self"}
        for key in COINBASE_WS_SHAPE_DECODER_SPEC:
            assert key in valid_params, (
                f"Key '{key}' not a JsonEnvelopeDecoder parameter"
            )

    def test_can_instantiate_json_envelope_decoder(self) -> None:
        decoder = JsonEnvelopeDecoder(**COINBASE_WS_SHAPE_DECODER_SPEC)
        assert decoder is not None

    def test_required_keys_present(self) -> None:
        # channel_field, pair_field, payload_field, kind_dispatch are required
        for key in ("channel_field", "payload_field", "kind_dispatch"):
            assert key in COINBASE_WS_SHAPE_DECODER_SPEC, f"Missing key: {key}"
