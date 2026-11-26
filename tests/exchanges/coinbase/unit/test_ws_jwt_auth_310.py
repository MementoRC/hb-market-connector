"""Acceptance tests for #310 PR 1: Coinbase WS JWT auth (CoinbaseJwtWsAuth).

Red until ``market_connector.exchanges.coinbase.ws_auth`` exists.
"""

from __future__ import annotations

import base64
import copy
import json
import unittest.mock as mock
from typing import Any
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.transport.ws_base import WsConnectorBase
from market_connector.ws_models.auth_models import WsAuthModel
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind

API_KEY = "organizations/org-1/apiKeys/key-1"
NOW = 1_700_000_000.0


@pytest.fixture(scope="session")
def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(scope="session")
def private_pem(ec_key: ec.EllipticCurvePrivateKey) -> str:
    return ec_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def public_pem(ec_key: ec.EllipticCurvePrivateKey) -> str:
    return (
        ec_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture
def auth(private_pem: str) -> Any:
    from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth

    return CoinbaseJwtWsAuth(API_KEY, private_pem, clock=lambda: NOW)


def _decode(token: str, public_pem: str) -> dict[str, Any]:
    """Verify signature with the public key; exp/nbf are checked manually against NOW."""
    return pyjwt.decode(
        token,
        public_pem,
        algorithms=["ES256"],
        options={"verify_exp": False, "verify_nbf": False, "verify_aud": False},
    )


class TestMakeJwt:
    def test_header(self, auth: Any) -> None:
        header = pyjwt.get_unverified_header(auth.make_jwt())
        assert header["alg"] == "ES256"
        assert header["kid"] == API_KEY
        assert header["nonce"]

    def test_nonce_differs_per_call(self, auth: Any) -> None:
        h1 = pyjwt.get_unverified_header(auth.make_jwt())
        h2 = pyjwt.get_unverified_header(auth.make_jwt())
        assert h1["nonce"] != h2["nonce"]

    def test_claims_and_signature(self, auth: Any, public_pem: str) -> None:
        claims = _decode(auth.make_jwt(), public_pem)
        assert claims["sub"] == API_KEY
        assert claims["iss"] == "cdp"
        assert claims["nbf"] == int(NOW)
        assert claims["exp"] == int(NOW) + 120

    def test_no_uri_claim(self, auth: Any, public_pem: str) -> None:
        assert "uri" not in _decode(auth.make_jwt(), public_pem)

    def test_clock_is_injected(self, private_pem: str, public_pem: str) -> None:
        from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth

        later = CoinbaseJwtWsAuth(API_KEY, private_pem, clock=lambda: NOW + 1000.9)
        claims = _decode(later.make_jwt(), public_pem)
        assert claims["nbf"] == int(NOW + 1000.9)
        assert claims["exp"] == int(NOW + 1000.9) + 120

    def test_wrong_key_fails_verification(self, auth: Any) -> None:
        other = ec.generate_private_key(ec.SECP256R1()).public_key()
        other_pem = other.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        with pytest.raises(pyjwt.InvalidSignatureError):
            _decode(auth.make_jwt(), other_pem.decode())


class TestTransformOutgoing:
    @pytest.mark.parametrize("msg_type", ["subscribe", "unsubscribe"])
    async def test_injects_jwt(self, auth: Any, public_pem: str, msg_type: str) -> None:
        msg = {"type": msg_type, "channel": "user", "product_ids": ["BTC-USD"]}
        out = await auth.transform_outgoing(msg)
        assert out == {**msg, "jwt": out["jwt"]}
        assert _decode(out["jwt"], public_pem)["sub"] == API_KEY

    async def test_returns_new_dict_without_mutating_input(self, auth: Any) -> None:
        msg = {"type": "subscribe", "channel": "user", "product_ids": ["BTC-USD"]}
        snapshot = copy.deepcopy(msg)
        out = await auth.transform_outgoing(msg)
        assert out is not msg
        assert msg == snapshot
        assert "jwt" not in msg

    async def test_fresh_jwt_each_call(self, auth: Any) -> None:
        msg = {"type": "subscribe", "channel": "user", "product_ids": ["BTC-USD"]}
        a = await auth.transform_outgoing(msg)
        b = await auth.transform_outgoing(msg)
        assert a["jwt"] != b["jwt"]

    async def test_other_types_unchanged(self, auth: Any) -> None:
        msg = {"type": "ping", "channel": "heartbeats"}
        snapshot = copy.deepcopy(msg)
        out = await auth.transform_outgoing(msg)
        assert out == snapshot
        assert "jwt" not in out
        assert msg == snapshot

    async def test_message_without_type_unchanged(self, auth: Any) -> None:
        out = await auth.transform_outgoing({"channel": "user"})
        assert out == {"channel": "user"}


class TestProtocolAndWsBase:
    def test_satisfies_ws_auth_model(self, auth: Any) -> None:
        assert isinstance(auth, WsAuthModel)

    async def test_hooks_are_noops(self, auth: Any) -> None:
        assert await auth.prepare_connection("wss://x.example") == "wss://x.example"
        sent: list[dict] = []

        async def ws_send(msg: dict) -> None:
            sent.append(msg)

        await auth.on_connected(ws_send)
        await auth.refresh()
        assert sent == []

    async def test_ws_base_send_has_jwt(self, auth: Any) -> None:
        class _Decoder:
            def decode(self, raw: Any) -> NormalizedWsMessage:
                return NormalizedWsMessage(
                    kind=WsMessageKind.UNKNOWN, channel=None, pair=None, payload={}, error=None
                )

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, data: str) -> None:
                self.sent.append(data)

            async def close(self) -> None:
                pass

            async def ping(self) -> None:
                pass

            def __aiter__(self) -> _Ws:
                return self

            async def __anext__(self) -> str:
                import asyncio

                await asyncio.Event().wait()
                raise StopAsyncIteration

        fake = _Ws()
        connector = WsConnectorBase(
            url="wss://advanced-trade-ws-user.coinbase.com",
            ws_auth=auth,
            decoder=_Decoder(),  # type: ignore[arg-type]
        )
        with mock.patch(
            "market_connector.transport.ws_base.websockets.connect",
            AsyncMock(return_value=fake),
        ):
            await connector.connect()
        try:
            await connector.send(
                {"type": "subscribe", "channel": "user", "product_ids": ["BTC-USD"]}
            )
        finally:
            await connector.disconnect()
        frames = [json.loads(s) for s in fake.sent]
        subscribe = [f for f in frames if f.get("type") == "subscribe"]
        assert len(subscribe) == 1
        assert subscribe[0]["jwt"]
        assert subscribe[0]["product_ids"] == ["BTC-USD"]


class TestSecretValidation:
    def test_hmac_secret_rejected(self) -> None:
        from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth

        secret = base64.b64encode(b"not-a-pem-key-just-hmac-bytes").decode()
        with pytest.raises(ValueError, match=r"(?i)pem|ec"):
            CoinbaseJwtWsAuth(API_KEY, secret)

    def test_non_ec_pem_rejected(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth

        rsa_pem = (
            rsa.generate_private_key(65537, 2048)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        with pytest.raises(ValueError, match=r"(?i)pem|ec"):
            CoinbaseJwtWsAuth(API_KEY, rsa_pem)


class TestConfigWsUserUrl:
    def test_ws_user_url_default(self) -> None:
        cfg = CoinbaseConfig(api_key="k", secret_key="s")
        assert cfg.ws_user_url == "wss://advanced-trade-ws-user.coinbase.com"

    def test_ws_url_unchanged(self) -> None:
        cfg = CoinbaseConfig(api_key="k", secret_key="s")
        assert cfg.ws_url == "wss://advanced-trade-ws.coinbase.com"
        sandbox = CoinbaseConfig(api_key="k", secret_key="s", sandbox=True)
        assert sandbox.ws_url == "wss://advanced-trade-ws-sandbox.coinbase.com"
