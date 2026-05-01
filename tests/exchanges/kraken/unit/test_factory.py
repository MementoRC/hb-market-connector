"""Unit tests for Kraken factory functions."""

from __future__ import annotations

import pytest

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.auth.spec import HmacSigningSpec
from market_connector.exchanges.kraken.factory import kraken_signer_factory, kraken_ws_auth_factory
from market_connector.exchanges.kraken.specs import KRAKEN_HMAC_SPEC, KRAKEN_PUBLIC_WS_AUTH
from market_connector.ws_models.auth_models import (
    PassThroughAuth,
    TokenFetchAuth,
    TokenInjectStrategy,
)

# Minimal valid Kraken API credentials for unit tests (not real keys).
# base64-encoded 64-byte secret (valid base64, not a real secret)
_TEST_API_KEY = "test-api-key-12345"
_TEST_SECRET = "dGVzdC1zZWNyZXQtZm9yLXVuaXQtdGVzdHMtb25seS10aGlzLWlzLW5vdC1yZWFsLXNlY3JldA=="


class TestKrakenSignerFactory:
    def test_returns_declarative_rest_signer(self) -> None:
        signer = kraken_signer_factory(_TEST_API_KEY, _TEST_SECRET)
        assert isinstance(signer, DeclarativeRestSigner)

    def test_uses_kraken_hmac_spec(self) -> None:
        signer = kraken_signer_factory(_TEST_API_KEY, _TEST_SECRET)
        assert isinstance(signer, DeclarativeRestSigner)
        assert isinstance(signer._spec, HmacSigningSpec)
        assert signer._spec is KRAKEN_HMAC_SPEC

    def test_raises_on_empty_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            kraken_signer_factory("", _TEST_SECRET)

    def test_raises_on_empty_secret_key(self) -> None:
        with pytest.raises(ValueError, match="secret_key"):
            kraken_signer_factory(_TEST_API_KEY, "")

    def test_different_keys_produce_different_signers(self) -> None:
        signer1 = kraken_signer_factory("key1", _TEST_SECRET)
        signer2 = kraken_signer_factory("key2", _TEST_SECRET)
        # Each signer is a distinct object
        assert signer1 is not signer2

    def test_signer_satisfies_signer_protocol(self) -> None:
        from market_connector.auth.protocols import Signer

        signer = kraken_signer_factory(_TEST_API_KEY, _TEST_SECRET)
        assert isinstance(signer, Signer)


class TestKrakenWsAuthFactory:
    def test_returns_tuple_of_two(self) -> None:
        result = kraken_ws_auth_factory()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_first_element_is_passthrough(self) -> None:
        public_auth, _ = kraken_ws_auth_factory()
        assert isinstance(public_auth, PassThroughAuth)

    def test_first_element_is_same_singleton(self) -> None:
        public_auth, _ = kraken_ws_auth_factory()
        assert public_auth is KRAKEN_PUBLIC_WS_AUTH

    def test_second_element_is_token_fetch_auth(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert isinstance(private_auth, TokenFetchAuth)

    def test_private_auth_endpoint(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert private_auth.token_endpoint == "/0/private/GetWebSocketsToken"

    def test_private_auth_response_path(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert private_auth.token_response_path == "result.token"

    def test_private_auth_ttl(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert private_auth.token_ttl_seconds == 900

    def test_private_auth_inject_strategy(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert private_auth.inject_strategy == TokenInjectStrategy.SUBSCRIBE_PAYLOAD

    def test_rest_client_bound_when_provided(self) -> None:
        mock_client = object()
        _, private_auth = kraken_ws_auth_factory(rest_client=mock_client)
        assert private_auth.rest_client is mock_client

    def test_rest_client_is_none_by_default(self) -> None:
        _, private_auth = kraken_ws_auth_factory()
        assert private_auth.rest_client is None

    def test_factory_creates_new_private_auth_each_call(self) -> None:
        _, auth1 = kraken_ws_auth_factory()
        _, auth2 = kraken_ws_auth_factory()
        # Each call returns a distinct (replaced) dataclass instance
        assert auth1 is not auth2
