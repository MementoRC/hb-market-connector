"""Coinbase Advanced Trade WebSocket JWT auth (#310).

Each ``subscribe``/``unsubscribe`` frame carries a freshly signed ES256 JWT
(2 minute lifetime, per-token nonce, no ``uri`` claim) as its ``jwt`` field.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from market_connector.exchanges.coinbase._auth_helpers import _try_normalize_pem

if TYPE_CHECKING:
    from collections.abc import Callable

    from market_connector.ws_models.auth_models import SendCallable

_JWT_TTL_SECONDS = 120
_SIGNED_TYPES = frozenset({"subscribe", "unsubscribe"})


class CoinbaseJwtWsAuth:
    """WsAuthModel injecting a fresh per-message JWT into subscribe/unsubscribe frames."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        pem = _try_normalize_pem(secret_key)
        if pem is None:
            raise ValueError("Coinbase WS JWT auth requires an EC private key in PEM format")
        key = serialization.load_pem_private_key(
            pem.encode(), password=None, backend=default_backend()
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("Coinbase WS JWT auth requires an EC private key in PEM format")
        self._api_key = api_key
        self._pem = pem
        self._clock = clock

    def make_jwt(self) -> str:
        now = int(self._clock())
        claims = {
            "sub": self._api_key,
            "iss": "cdp",
            "nbf": now,
            "exp": now + _JWT_TTL_SECONDS,
        }
        headers = {"kid": self._api_key, "nonce": secrets.token_hex(16)}
        token: str = pyjwt.encode(  # type: ignore[assignment]  # PyJWT >=2.x returns str; stubs may lag
            claims, self._pem, algorithm="ES256", headers=headers
        )
        return token

    async def prepare_connection(self, base_url: str) -> str:
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        pass

    async def transform_outgoing(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") in _SIGNED_TYPES:
            return {**msg, "jwt": self.make_jwt()}
        return msg

    async def refresh(self) -> None:
        pass
