"""WS auth model Protocol and 5 declarative implementations (spec §8).

Provides:
  - WsAuthModel Protocol — 4 lifecycle hooks
  - SendCallable type alias
  - TokenInjectStrategy enum
  - SignedLoginMessageAuth  — sends signed login message on connect
  - PerMessageSignAuth      — injects fresh sig into every outgoing message
  - TokenFetchAuth          — fetches REST token; injects per strategy
  - ListenKeyAuth           — POSTs for listen-key; rewrites URL
  - PassThroughAuth         — all hooks no-op
  - build_ws_auth()         — factory dispatch by spec type
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from market_connector.auth.substitute import Surface, substitute

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

SendCallable = Callable[[dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# TokenInjectStrategy
# ---------------------------------------------------------------------------


class TokenInjectStrategy(Enum):
    URL_QUERY = "URL_QUERY"
    SUBSCRIBE_PAYLOAD = "SUBSCRIBE_PAYLOAD"
    CONNECT_HEADER = "CONNECT_HEADER"


# ---------------------------------------------------------------------------
# RestClient Protocol — minimal duck-typed interface for REST calls
# ---------------------------------------------------------------------------


@runtime_checkable
class RestClient(Protocol):
    """Minimal REST client interface consumed by TokenFetchAuth and ListenKeyAuth.

    Any object with matching async ``get``/``post``/``put`` signatures satisfies
    this Protocol — no concrete transport coupling.
    """

    async def get(self, endpoint: str, **kwargs: Any) -> dict: ...

    async def post(self, endpoint: str, **kwargs: Any) -> dict: ...

    async def put(self, endpoint: str, **kwargs: Any) -> dict: ...


# ---------------------------------------------------------------------------
# WsAuthModel Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class WsAuthModel(Protocol):
    """Lifecycle hooks for WS authentication.

    Each WsConnectorBase holds one WsAuthModel instance and calls the 4 hooks
    at the appropriate points:
      - prepare_connection: before opening the socket (may rewrite URL)
      - on_connected: immediately after socket open (may send login frame)
      - transform_outgoing: for every outgoing message (may inject sig/token)
      - refresh: on TTL expiry / keep-alive timer
    """

    async def prepare_connection(self, base_url: str) -> str:
        """Pre-connect hook. Returns (possibly rewritten) URL."""
        ...

    async def on_connected(self, ws_send: SendCallable) -> None:
        """Post-connect hook. May send a signed login message."""
        ...

    async def transform_outgoing(self, msg: dict) -> dict:
        """Per-message hook. Returns (possibly augmented) message dict."""
        ...

    async def refresh(self) -> None:
        """TTL refresh hook. Re-fetches tokens or PUTs keepalive."""
        ...


# ---------------------------------------------------------------------------
# Signer duck-typed interface — only the WS-specific method we need
# ---------------------------------------------------------------------------


class _WsSigner(Protocol):
    """Structural interface for signers injected into WS auth models."""

    api_key: str

    async def sign_ws(self, sig_input: str) -> str: ...


# ---------------------------------------------------------------------------
# Helper: resolve dot-separated path in nested dict
# ---------------------------------------------------------------------------


def _resolve_path(data: dict, path: str) -> str:
    """Walk a dot-separated path and return the leaf value as str."""
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return str(node)


# ---------------------------------------------------------------------------
# SignedLoginMessageAuth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedLoginMessageAuth:
    """Sends a single signed login message immediately after WS connect.

    Used by ~12 connectors: bybit, okx, bitget, kucoin_perp, htx, bitmart, …

    The login payload template is a dict with {api_key}, {ts}, {sig} placeholders
    in its *values* (not keys). Template expansion uses direct str.format_map
    because the OUTPUT surface covers {sig} while WS covers {api_key}/{ts}.
    """

    login_payload_template: dict
    sig_input_template: str
    signer: Any  # _WsSigner structural type — avoids Protocol import coupling

    async def prepare_connection(self, base_url: str) -> str:
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        ts = str(int(time.time() * 1000))
        sig_input = substitute(self.sig_input_template, {"ts": ts}, surface=Surface.WS)
        assert isinstance(sig_input, str)
        sig = await self.signer.sign_ws(sig_input)
        ctx = {"api_key": self.signer.api_key, "ts": ts, "sig": sig}
        payload = _expand_dict_template(self.login_payload_template, ctx)
        await ws_send(payload)

    async def transform_outgoing(self, msg: dict) -> dict:
        return msg

    async def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# PerMessageSignAuth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerMessageSignAuth:
    """Injects a fresh signature into every outgoing WS message.

    Used by ~7 connectors: gate_io, coinbase legacy, aevo, dexalot, …

    inject_fields maps output field name → template string, e.g.::

        {"signature": "{sig}", "timestamp": "{ts}"}

    Templates may reference WS-surface variables ({api_key}, {ts}, {nonce},
    {rand_hex}) and the OUTPUT-surface variable {sig}.
    """

    sig_input_template: str
    inject_fields: dict[str, str]
    signer: Any  # _WsSigner structural type

    async def prepare_connection(self, base_url: str) -> str:
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        pass

    async def transform_outgoing(self, msg: dict) -> dict:
        ts = str(int(time.time() * 1000))
        sig_input = substitute(
            self.sig_input_template, {"ts": ts, "api_key": self.signer.api_key}, surface=Surface.WS
        )
        assert isinstance(sig_input, str)
        sig = await self.signer.sign_ws(sig_input)
        ctx = {"api_key": self.signer.api_key, "ts": ts, "sig": sig}
        result = dict(msg)
        for out_field, template in self.inject_fields.items():
            result[out_field] = template.format_map(ctx)
        return result

    async def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# TokenFetchAuth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenFetchAuth:
    """Fetches an auth token from a REST endpoint; injects per TokenInjectStrategy.

    Used by ~4 connectors: kucoin, derive, architect, …

    The token is cached in a mutable side-car (``_state``) held as a dataclass
    field with ``field(default_factory=…, compare=False)``. Frozen dataclasses
    cannot hold mutable attributes directly, but they *can* hold mutable
    container references — the container itself is never reassigned.
    """

    token_endpoint: str
    token_response_path: str
    token_ttl_seconds: int
    inject_strategy: TokenInjectStrategy
    rest_client: Any  # RestClient structural type
    _state: dict = field(default_factory=dict, compare=False, repr=False)

    async def prepare_connection(self, base_url: str) -> str:
        await self._fetch_token()
        if self.inject_strategy is TokenInjectStrategy.URL_QUERY:
            sep = "&" if "?" in base_url else "?"
            return f"{base_url}{sep}token={self._state['token']}"
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        pass

    async def transform_outgoing(self, msg: dict) -> dict:
        if self.inject_strategy is TokenInjectStrategy.SUBSCRIBE_PAYLOAD:
            result = dict(msg)
            result["token"] = self._state.get("token", "")
            return result
        return msg

    async def refresh(self) -> None:
        await self._fetch_token()

    async def _fetch_token(self) -> None:
        response = await self.rest_client.get(self.token_endpoint)
        self._state["token"] = _resolve_path(response, self.token_response_path)
        self._state["fetched_at"] = time.time()


# ---------------------------------------------------------------------------
# ListenKeyAuth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListenKeyAuth:
    """POSTs to obtain a Binance-style listen key; rewrites the WS URL.

    Used by ~3 connectors: binance, binance_perpetual, mexc.

    The listen key is stored in a mutable ``_state`` side-car (same pattern
    as TokenFetchAuth).  keepalive_endpoint=None suppresses the PUT in refresh().
    """

    listen_key_endpoint: str
    listen_key_response_path: str
    url_template: str
    keepalive_endpoint: str | None
    keepalive_interval_seconds: int
    rest_client: Any  # RestClient structural type
    _state: dict = field(default_factory=dict, compare=False, repr=False)

    async def prepare_connection(self, base_url: str) -> str:
        response = await self.rest_client.post(self.listen_key_endpoint)
        listen_key = _resolve_path(response, self.listen_key_response_path)
        self._state["listen_key"] = listen_key
        # Use direct str.format_map — {listen_key} is not a spec §6.6 variable
        return self.url_template.format(listen_key=listen_key)

    async def on_connected(self, ws_send: SendCallable) -> None:
        pass

    async def transform_outgoing(self, msg: dict) -> dict:
        return msg

    async def refresh(self) -> None:
        if self.keepalive_endpoint is not None:
            await self.rest_client.put(self.keepalive_endpoint)


# ---------------------------------------------------------------------------
# PassThroughAuth
# ---------------------------------------------------------------------------


class PassThroughAuth:
    """All hooks are no-ops; used for public WS streams.

    Used by ~11 connectors: bitstamp, bitrue, ascend_ex, kraken-public, …
    Not a dataclass (no fields).
    """

    async def prepare_connection(self, base_url: str) -> str:
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        pass

    async def transform_outgoing(self, msg: dict) -> dict:
        return msg

    async def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_ws_auth(
    spec: Any,
    signer: Any,
    rest_client: Any = None,
) -> WsAuthModel:
    """Wire a WS auth spec into a ready-to-use WsAuthModel.

    Dispatches by isinstance check (spec §8.3).  Spec objects carry their
    own dependencies (signer/rest_client) when constructed by the caller;
    the factory simply returns ``spec`` after type-gating.

    Args:
        spec: One of the 5 WsAuthModel implementation instances.
        signer: REST Signer (unused for PassThrough/Token/ListenKey).
        rest_client: Optional RestClient (used by TokenFetchAuth/ListenKeyAuth).

    Raises:
        TypeError: If spec is not a recognised WsAuthModel implementation.
    """
    if isinstance(spec, PassThroughAuth):
        return spec
    if isinstance(spec, SignedLoginMessageAuth):
        return spec
    if isinstance(spec, PerMessageSignAuth):
        return spec
    if isinstance(spec, TokenFetchAuth):
        return spec
    if isinstance(spec, ListenKeyAuth):
        return spec
    raise TypeError(
        f"Unknown WsAuthModel spec type: {type(spec)!r}. "
        "Expected one of: PassThroughAuth, SignedLoginMessageAuth, "
        "PerMessageSignAuth, TokenFetchAuth, ListenKeyAuth."
    )


# ---------------------------------------------------------------------------
# Internal helper: recursively expand dict template values
# ---------------------------------------------------------------------------


def _expand_dict_template(template: dict, ctx: dict[str, str]) -> dict:
    """Recursively expand {var} placeholders in dict values using ctx.

    Only string values are expanded; lists, dicts, and other types are
    recursed into or left unchanged.
    """
    result: dict = {}
    for k, v in template.items():
        if isinstance(v, str):
            result[k] = v.format_map(ctx)
        elif isinstance(v, list):
            result[k] = [item.format_map(ctx) if isinstance(item, str) else item for item in v]
        elif isinstance(v, dict):
            result[k] = _expand_dict_template(v, ctx)
        else:
            result[k] = v
    return result
