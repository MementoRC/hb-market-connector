"""Tests for ws_models.auth_models — WsAuthModel Protocol and 5 implementations."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.ws_models.auth_models import (
    ListenKeyAuth,
    PassThroughAuth,
    PerMessageSignAuth,
    SendCallable,
    SignedLoginMessageAuth,
    TokenFetchAuth,
    TokenInjectStrategy,
    WsAuthModel,
    build_ws_auth,
)

# ---------------------------------------------------------------------------
# Type alias check
# ---------------------------------------------------------------------------


class TestSendCallableTypeAlias:
    def test_send_callable_is_callable_type(self) -> None:
        """SendCallable should be a Callable type alias usable in annotations."""
        # Verify it can be used as a type annotation without errors
        _: SendCallable = AsyncMock()  # type: ignore[assignment]
        assert callable(_)


# ---------------------------------------------------------------------------
# TokenInjectStrategy enum
# ---------------------------------------------------------------------------


class TestTokenInjectStrategyEnum:
    def test_has_url_query(self) -> None:
        assert hasattr(TokenInjectStrategy, "URL_QUERY")

    def test_has_subscribe_payload(self) -> None:
        assert hasattr(TokenInjectStrategy, "SUBSCRIBE_PAYLOAD")

    def test_has_connect_header(self) -> None:
        assert hasattr(TokenInjectStrategy, "CONNECT_HEADER")

    def test_three_members(self) -> None:
        assert len(TokenInjectStrategy) == 3


# ---------------------------------------------------------------------------
# WsAuthModel Protocol structural check
# ---------------------------------------------------------------------------


class TestWsAuthModelProtocol:
    def test_pass_through_satisfies_protocol(self) -> None:
        auth: WsAuthModel = PassThroughAuth()
        assert hasattr(auth, "prepare_connection")
        assert hasattr(auth, "on_connected")
        assert hasattr(auth, "transform_outgoing")
        assert hasattr(auth, "refresh")


# ---------------------------------------------------------------------------
# TestPassThroughAuth
# ---------------------------------------------------------------------------


class TestPassThroughAuth:
    @pytest.fixture
    def auth(self) -> PassThroughAuth:
        return PassThroughAuth()

    async def test_prepare_connection_returns_url_unchanged(self, auth: PassThroughAuth) -> None:
        result = await auth.prepare_connection("wss://example.com/ws")
        assert result == "wss://example.com/ws"

    async def test_on_connected_no_op(self, auth: PassThroughAuth) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        ws_send.assert_not_called()

    async def test_transform_outgoing_returns_msg_unchanged(self, auth: PassThroughAuth) -> None:
        msg = {"op": "subscribe", "channel": "ticker"}
        result = await auth.transform_outgoing(msg)
        assert result == msg

    async def test_refresh_no_op(self, auth: PassThroughAuth) -> None:
        # Should complete without error
        await auth.refresh()


# ---------------------------------------------------------------------------
# TestSignedLoginMessageAuth
# ---------------------------------------------------------------------------


class TestSignedLoginMessageAuth:
    @pytest.fixture
    def mock_signer(self) -> MagicMock:
        signer = MagicMock()
        signer.api_key = "test-api-key"
        signer.sign_ws = AsyncMock(return_value="mock-sig-value")
        return signer

    @pytest.fixture
    def auth(self, mock_signer: MagicMock) -> SignedLoginMessageAuth:
        return SignedLoginMessageAuth(
            login_payload_template={"op": "auth", "args": ["{api_key}", "{ts}", "{sig}"]},
            sig_input_template="{ts}GET/realtime",
            signer=mock_signer,
        )

    async def test_on_connected_sends_one_message(self, auth: SignedLoginMessageAuth) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        ws_send.assert_called_once()

    async def test_on_connected_payload_contains_api_key(
        self, auth: SignedLoginMessageAuth
    ) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        sent_payload = ws_send.call_args[0][0]
        assert sent_payload["args"][0] == "test-api-key"

    async def test_on_connected_payload_contains_sig(self, auth: SignedLoginMessageAuth) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        sent_payload = ws_send.call_args[0][0]
        assert sent_payload["args"][2] == "mock-sig-value"

    async def test_on_connected_payload_has_ts(self, auth: SignedLoginMessageAuth) -> None:
        ws_send: SendCallable = AsyncMock()
        before = int(time.time() * 1000) - 1000
        await auth.on_connected(ws_send)
        sent_payload = ws_send.call_args[0][0]
        ts_val = int(sent_payload["args"][1])
        # ts should be a reasonable epoch value (seconds or millis)
        assert ts_val > before

    async def test_prepare_connection_no_op(self, auth: SignedLoginMessageAuth) -> None:
        result = await auth.prepare_connection("wss://example.com/ws")
        assert result == "wss://example.com/ws"

    async def test_transform_outgoing_no_op(self, auth: SignedLoginMessageAuth) -> None:
        msg = {"op": "subscribe"}
        result = await auth.transform_outgoing(msg)
        assert result == msg

    async def test_refresh_no_op(self, auth: SignedLoginMessageAuth) -> None:
        await auth.refresh()

    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(SignedLoginMessageAuth)
        assert SignedLoginMessageAuth.__dataclass_params__.frozen  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestPerMessageSignAuth
# ---------------------------------------------------------------------------


class TestPerMessageSignAuth:
    @pytest.fixture
    def mock_signer(self) -> MagicMock:
        signer = MagicMock()
        signer.api_key = "key-abc"
        call_count = {"n": 0}

        async def sign_ws(sig_input: str) -> str:
            call_count["n"] += 1
            return f"sig-{call_count['n']}"

        signer.sign_ws = sign_ws
        return signer

    @pytest.fixture
    def auth(self, mock_signer: MagicMock) -> PerMessageSignAuth:
        return PerMessageSignAuth(
            sig_input_template="{ts}{api_key}",
            inject_fields={"signature": "{sig}", "timestamp": "{ts}"},
            signer=mock_signer,
        )

    async def test_transform_outgoing_injects_signature_field(
        self, auth: PerMessageSignAuth
    ) -> None:
        msg = {"op": "subscribe", "channel": "orderbook"}
        result = await auth.transform_outgoing(msg)
        assert "signature" in result

    async def test_transform_outgoing_injects_timestamp_field(
        self, auth: PerMessageSignAuth
    ) -> None:
        msg = {"op": "subscribe"}
        result = await auth.transform_outgoing(msg)
        assert "timestamp" in result

    async def test_transform_outgoing_preserves_original_fields(
        self, auth: PerMessageSignAuth
    ) -> None:
        msg = {"op": "subscribe", "channel": "trades"}
        result = await auth.transform_outgoing(msg)
        assert result["op"] == "subscribe"
        assert result["channel"] == "trades"

    async def test_ts_is_fresh_per_call(self, auth: PerMessageSignAuth) -> None:
        """Two calls should produce different {ts} values (at least not identical)."""
        msg = {"op": "subscribe"}
        # We can't guarantee different ms, so we mock time at signer level
        # Instead, verify the call is made twice (sig changes)
        result1 = await auth.transform_outgoing(msg)
        result2 = await auth.transform_outgoing(msg)
        # Signatures are call-count-based mocks: each call yields a different sig
        assert result1["signature"] != result2["signature"]

    async def test_prepare_connection_no_op(self, auth: PerMessageSignAuth) -> None:
        result = await auth.prepare_connection("wss://example.com")
        assert result == "wss://example.com"

    async def test_on_connected_no_op(self, auth: PerMessageSignAuth) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        ws_send.assert_not_called()

    async def test_refresh_no_op(self, auth: PerMessageSignAuth) -> None:
        await auth.refresh()

    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(PerMessageSignAuth)
        assert PerMessageSignAuth.__dataclass_params__.frozen  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestTokenFetchAuth
# ---------------------------------------------------------------------------


class TestTokenFetchAuth:
    @pytest.fixture
    def mock_rest_client(self) -> MagicMock:
        client = MagicMock()
        client.get = AsyncMock(return_value={"data": {"token": "fetched-token-xyz"}})
        return client

    def _make_auth(
        self,
        strategy: TokenInjectStrategy,
        mock_rest_client: MagicMock,
    ) -> TokenFetchAuth:
        return TokenFetchAuth(
            token_endpoint="/api/v1/ws_token",
            token_response_path="data.token",
            token_ttl_seconds=300,
            inject_strategy=strategy,
            rest_client=mock_rest_client,
        )

    async def test_prepare_connection_fetches_token(self, mock_rest_client: MagicMock) -> None:
        auth = self._make_auth(TokenInjectStrategy.URL_QUERY, mock_rest_client)
        await auth.prepare_connection("wss://example.com/ws")
        mock_rest_client.get.assert_called_once()

    async def test_prepare_connection_url_query_appends_token(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.URL_QUERY, mock_rest_client)
        result = await auth.prepare_connection("wss://example.com/ws")
        assert "fetched-token-xyz" in result
        assert "token=" in result

    async def test_prepare_connection_subscribe_payload_returns_url_unchanged(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.SUBSCRIBE_PAYLOAD, mock_rest_client)
        result = await auth.prepare_connection("wss://example.com/ws")
        assert result == "wss://example.com/ws"

    async def test_prepare_connection_connect_header_returns_url_unchanged(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.CONNECT_HEADER, mock_rest_client)
        result = await auth.prepare_connection("wss://example.com/ws")
        assert result == "wss://example.com/ws"

    async def test_transform_outgoing_subscribe_payload_injects_token(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.SUBSCRIBE_PAYLOAD, mock_rest_client)
        await auth.prepare_connection("wss://example.com/ws")
        msg = {"op": "subscribe", "channel": "ticker"}
        result = await auth.transform_outgoing(msg)
        assert result.get("token") == "fetched-token-xyz"

    async def test_transform_outgoing_url_query_does_not_inject_into_msg(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.URL_QUERY, mock_rest_client)
        await auth.prepare_connection("wss://example.com/ws")
        msg = {"op": "subscribe"}
        result = await auth.transform_outgoing(msg)
        assert "token" not in result

    async def test_transform_outgoing_connect_header_does_not_inject_into_msg(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = self._make_auth(TokenInjectStrategy.CONNECT_HEADER, mock_rest_client)
        await auth.prepare_connection("wss://example.com/ws")
        msg = {"op": "subscribe"}
        result = await auth.transform_outgoing(msg)
        assert "token" not in result

    async def test_refresh_re_fetches_token(self, mock_rest_client: MagicMock) -> None:
        auth = self._make_auth(TokenInjectStrategy.SUBSCRIBE_PAYLOAD, mock_rest_client)
        await auth.prepare_connection("wss://example.com/ws")
        await auth.refresh()
        assert mock_rest_client.get.call_count == 2

    async def test_on_connected_no_op(self, mock_rest_client: MagicMock) -> None:
        auth = self._make_auth(TokenInjectStrategy.SUBSCRIBE_PAYLOAD, mock_rest_client)
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        ws_send.assert_not_called()

    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(TokenFetchAuth)
        assert TokenFetchAuth.__dataclass_params__.frozen  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestListenKeyAuth
# ---------------------------------------------------------------------------


class TestListenKeyAuth:
    @pytest.fixture
    def mock_rest_client(self) -> MagicMock:
        client = MagicMock()
        client.post = AsyncMock(return_value={"listenKey": "test-listen-key-abc"})
        client.put = AsyncMock(return_value={})
        return client

    @pytest.fixture
    def auth(self, mock_rest_client: MagicMock) -> ListenKeyAuth:
        return ListenKeyAuth(
            listen_key_endpoint="/api/v3/userDataStream",
            listen_key_response_path="listenKey",
            url_template="wss://stream.binance.com/ws/{listen_key}",
            keepalive_endpoint="/api/v3/userDataStream",
            keepalive_interval_seconds=1800,
            rest_client=mock_rest_client,
        )

    async def test_prepare_connection_posts_to_listen_key_endpoint(
        self, auth: ListenKeyAuth, mock_rest_client: MagicMock
    ) -> None:
        await auth.prepare_connection("wss://stream.binance.com/ws")
        mock_rest_client.post.assert_called_once()

    async def test_prepare_connection_returns_rewritten_url(self, auth: ListenKeyAuth) -> None:
        result = await auth.prepare_connection("wss://stream.binance.com/ws")
        assert result == "wss://stream.binance.com/ws/test-listen-key-abc"

    async def test_prepare_connection_substitutes_listen_key_in_template(
        self, auth: ListenKeyAuth
    ) -> None:
        result = await auth.prepare_connection("wss://stream.binance.com/ws")
        assert "test-listen-key-abc" in result
        assert "{listen_key}" not in result

    async def test_refresh_puts_to_keepalive_endpoint(
        self, auth: ListenKeyAuth, mock_rest_client: MagicMock
    ) -> None:
        # Must prepare_connection first to have a listen key
        await auth.prepare_connection("wss://stream.binance.com/ws")
        await auth.refresh()
        mock_rest_client.put.assert_called_once()

    async def test_refresh_no_op_when_no_keepalive_endpoint(
        self, mock_rest_client: MagicMock
    ) -> None:
        auth = ListenKeyAuth(
            listen_key_endpoint="/api/v3/userDataStream",
            listen_key_response_path="listenKey",
            url_template="wss://stream.binance.com/ws/{listen_key}",
            keepalive_endpoint=None,
            keepalive_interval_seconds=1800,
            rest_client=mock_rest_client,
        )
        await auth.prepare_connection("wss://stream.binance.com/ws")
        await auth.refresh()
        mock_rest_client.put.assert_not_called()

    async def test_transform_outgoing_no_op(self, auth: ListenKeyAuth) -> None:
        await auth.prepare_connection("wss://stream.binance.com/ws")
        msg = {"method": "SUBSCRIBE", "params": ["btcusdt@trade"]}
        result = await auth.transform_outgoing(msg)
        assert result == msg

    async def test_on_connected_no_op(
        self, auth: ListenKeyAuth, mock_rest_client: MagicMock
    ) -> None:
        ws_send: SendCallable = AsyncMock()
        await auth.on_connected(ws_send)
        ws_send.assert_not_called()

    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(ListenKeyAuth)
        assert ListenKeyAuth.__dataclass_params__.frozen  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestBuildWsAuth factory
# ---------------------------------------------------------------------------


class TestBuildWsAuthFactory:
    def test_pass_through_spec_returns_pass_through_auth(self) -> None:
        result = build_ws_auth(spec=PassThroughAuth(), signer=None)
        assert isinstance(result, PassThroughAuth)

    def test_signed_login_spec_returns_signed_login_auth(self) -> None:
        mock_signer = MagicMock()
        spec = SignedLoginMessageAuth(
            login_payload_template={"op": "auth", "args": ["{api_key}", "{ts}", "{sig}"]},
            sig_input_template="{ts}GET/realtime",
            signer=mock_signer,
        )
        result = build_ws_auth(spec=spec, signer=mock_signer)
        assert isinstance(result, SignedLoginMessageAuth)

    def test_per_message_sign_spec_returns_per_message_auth(self) -> None:
        mock_signer = MagicMock()
        spec = PerMessageSignAuth(
            sig_input_template="{ts}{api_key}",
            inject_fields={"signature": "{sig}"},
            signer=mock_signer,
        )
        result = build_ws_auth(spec=spec, signer=mock_signer)
        assert isinstance(result, PerMessageSignAuth)

    def test_token_fetch_spec_returns_token_fetch_auth(self) -> None:
        mock_rest = MagicMock()
        spec = TokenFetchAuth(
            token_endpoint="/token",
            token_response_path="data.token",
            token_ttl_seconds=300,
            inject_strategy=TokenInjectStrategy.URL_QUERY,
            rest_client=mock_rest,
        )
        result = build_ws_auth(spec=spec, signer=None, rest_client=mock_rest)
        assert isinstance(result, TokenFetchAuth)

    def test_listen_key_spec_returns_listen_key_auth(self) -> None:
        mock_rest = MagicMock()
        spec = ListenKeyAuth(
            listen_key_endpoint="/userDataStream",
            listen_key_response_path="listenKey",
            url_template="wss://stream.binance.com/ws/{listen_key}",
            keepalive_endpoint=None,
            keepalive_interval_seconds=1800,
            rest_client=mock_rest,
        )
        result = build_ws_auth(spec=spec, signer=None, rest_client=mock_rest)
        assert isinstance(result, ListenKeyAuth)

    def test_unknown_spec_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            build_ws_auth(spec="unsupported", signer=None)  # type: ignore[arg-type]
