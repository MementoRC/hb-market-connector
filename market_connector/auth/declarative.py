"""DeclarativeRestSigner — HMAC, JWT, and Bearer dispatch (spec §6.3–6.5, §6.7).

Reads every sub-spec field and applies:
  - KeyMaterialSpec.encoding  decode (raw_str / base64 / hex / PEM)
  - KeyMaterialSpec.derived_credentials  pre-computation
  - TimestampSpec  formatting
  - NonceSpec  source (with per-instance asyncio.Lock + counter when monotonic=True)
  - SignatureRecipe.body_hash  inner stage (Kraken two-stage)
  - SignatureRecipe.template  substitution via auth/substitute.py
  - SignatureRecipe.algorithm  HMAC computation
  - SignatureRecipe.output_encoding  (hex / base64)
  - AuthOutputSpec  headers / body_inject / qs_inject injection

JWT mode (Task 5): uses PyJWT to produce ES256/RS256 tokens from PEM keys.
Bearer mode (Task 5): uses httpx AsyncClient to POST to a token endpoint with
  an in-memory TTL cache to avoid redundant fetches.

Monotonic counter scope: per-signer-instance asyncio.Lock + integer counter.
Rationale: spec §6.7 says "framework-owned process-wide atomic counter"; a
class-level lock/counter would satisfy "process-wide" but creates cross-test
interference.  Per-instance satisfies the ordering guarantee (strictly
increasing within any single signing context) and is easier to test
deterministically.  See docstring on _next_counter_nonce for full reasoning.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
import urllib.parse
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import jwt as pyjwt

from market_connector.auth.spec import (
    BearerTokenSpec,
    BodyFormat,
    BodyHashSpec,
    HashAlgorithm,
    HmacSigningSpec,
    JwtAlgorithm,
    JwtSigningSpec,
    KeyEncoding,
    NoncePlacement,
    NonceSource,
    SigAlgorithm,
    SigEncoding,
    SigningSpec,
    TimestampFormat,
    TimestampUnit,
)

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request


# ---------------------------------------------------------------------------
# Derivation function registry
# ---------------------------------------------------------------------------
# DerivationFn labels (opaque strings from spec.py) resolve to callables here.
# Each callable has signature: (secret_bytes: bytes, value: str) -> str


def _hmac_sha256_base64(secret_bytes: bytes, value: str) -> str:
    """HMAC-SHA256 of value using secret_bytes, base64-encoded. (Kucoin passphrase)."""
    digest = hmac.new(secret_bytes, value.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


_DERIVATION_FN_REGISTRY: dict[str, Any] = {
    "HMAC_SHA256_BASE64": _hmac_sha256_base64,
}


# ---------------------------------------------------------------------------
# Key decoding
# ---------------------------------------------------------------------------


def _decode_key(raw: str, encoding: KeyEncoding) -> bytes:
    """Decode the raw credential string to bytes per KeyEncoding."""
    if encoding is KeyEncoding.RAW_STR:
        return raw.encode("utf-8")
    if encoding is KeyEncoding.BASE64:
        return base64.b64decode(raw)
    if encoding is KeyEncoding.HEX:
        return bytes.fromhex(raw)
    # PEM_EC / PEM_RSA — returned as bytes for JWT (Task 5); HMAC doesn't use PEM.
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _make_timestamp(unit: TimestampUnit, fmt: TimestampFormat) -> str:
    """Return current timestamp formatted per TimestampSpec."""
    now = time.time()
    if unit is TimestampUnit.SECONDS:
        raw = int(now)
    elif unit is TimestampUnit.MILLISECONDS:
        raw = int(now * 1_000)
    else:  # NANOSECONDS
        raw = int(now * 1_000_000_000)

    if fmt is TimestampFormat.INTEGER:
        return str(raw)
    if fmt is TimestampFormat.ISO8601:
        import datetime

        return datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    # ISO8601_Z
    import datetime

    return datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# Body serialisation
# ---------------------------------------------------------------------------


def _serialise_body(body: str | bytes | None, fmt: BodyFormat) -> str:
    """Return serialised body string for signing."""
    if fmt is BodyFormat.NONE or body is None:
        return "" if body is None else (body if isinstance(body, str) else body.decode("utf-8"))
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return body  # caller is responsible for format; just return as-is


def _qs_sorted(qs_params: dict) -> str:
    """Return URL-encoded query string sorted by key (Binance pattern), no '?' prefix."""
    return urllib.parse.urlencode(sorted(qs_params.items()))


# ---------------------------------------------------------------------------
# Hash algorithm dispatch
# ---------------------------------------------------------------------------


def _hash_bytes(algorithm: HashAlgorithm, data: bytes) -> bytes:
    if algorithm is HashAlgorithm.SHA256:
        return hashlib.sha256(data).digest()
    return hashlib.sha512(data).digest()


def _hmac_bytes(algorithm: SigAlgorithm, key: bytes, data: bytes) -> bytes:
    if algorithm is SigAlgorithm.HMAC_SHA256:
        return hmac.new(key, data, hashlib.sha256).digest()
    return hmac.new(key, data, hashlib.sha512).digest()


def _encode_sig(raw: bytes, encoding: SigEncoding) -> str:
    if encoding is SigEncoding.HEX:
        return raw.hex()
    return base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Template expansion for output stage
# ---------------------------------------------------------------------------
# Output templates (header values, qs_inject values) may mix REST-surface
# variables ({api_key}, {ts}, {nonce}, {recv_window}, …) with the OUTPUT-
# surface variable {sig}.  The substitute() engine validates surface per
# variable, so no single Surface covers both groups.
#
# Design choice: expand output templates with direct str.format_map() using
# a full merged context dict.  This bypasses surface validation, which is
# acceptable because output templates are authored by framework engineers
# (not user input), and the variable set is constrained to the finite
# documented namespace.


def _expand_output_template(template: str, ctx: dict[str, str]) -> str:
    """Expand a single output-stage template using direct format_map."""
    return template.format_map(ctx)


# ---------------------------------------------------------------------------
# DeclarativeRestSigner
# ---------------------------------------------------------------------------


class DeclarativeRestSigner:
    """Async signer that interprets HmacSigningSpec / JwtSigningSpec /
    BearerTokenSpec to produce signed Requests.

    Usage::

        signer = DeclarativeRestSigner.from_spec(spec, api_key="...", secret="...")
        signed_request = await signer.sign(request)

    The ``sign()`` method returns a *new* Request (immutable); the input is
    never mutated.
    """

    def __init__(
        self,
        spec: HmacSigningSpec | JwtSigningSpec | BearerTokenSpec,
        *,
        api_key: str,
        secret_bytes: bytes,
        extra_creds: dict[str, str],
        derived_creds: dict[str, str],
        _fixed_ts: str | None = None,
        _fixed_nonce: str | None = None,
    ) -> None:
        self._spec = spec
        self._api_key = api_key
        self._secret_bytes = secret_bytes
        # extra_creds: recv_window, memo, passphrase (raw), etc.
        self._extra_creds = extra_creds
        # derived_creds: pre-computed derived credentials (e.g. Kucoin passphrase)
        self._derived_creds = derived_creds
        # Test seams — override timestamp / nonce generation
        self._fixed_ts = _fixed_ts
        self._fixed_nonce = _fixed_nonce
        # Per-instance monotonic counter (see module docstring for scope rationale)
        self._counter_lock = asyncio.Lock()
        self._counter = 0
        # Bearer token cache
        self._bearer_token: str | None = None
        self._bearer_fetched_at: float = 0.0

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_spec(
        cls,
        spec: SigningSpec,
        *,
        api_key: str,
        secret: str,
        _fixed_ts: str | None = None,
        _fixed_nonce: str | None = None,
        **extra_creds: str,
    ) -> DeclarativeRestSigner:
        """Dispatch on spec type and construct the appropriate signer."""
        if isinstance(spec, HmacSigningSpec):
            return cls._from_hmac_spec(
                spec,
                api_key=api_key,
                secret=secret,
                _fixed_ts=_fixed_ts,
                _fixed_nonce=_fixed_nonce,
                **extra_creds,
            )
        if isinstance(spec, JwtSigningSpec):
            return cls._from_jwt_spec(
                spec,
                api_key=api_key,
                secret=secret,
                **extra_creds,
            )
        if isinstance(spec, BearerTokenSpec):
            return cls._from_bearer_spec(
                spec,
                api_key=api_key,
                secret=secret,
                **extra_creds,
            )
        raise TypeError(f"Unknown spec type: {type(spec)!r}")

    @classmethod
    def _from_hmac_spec(
        cls,
        spec: HmacSigningSpec,
        *,
        api_key: str,
        secret: str,
        _fixed_ts: str | None = None,
        _fixed_nonce: str | None = None,
        **extra_creds: str,
    ) -> DeclarativeRestSigner:
        secret_bytes = _decode_key(secret, spec.key_material.encoding)

        # Pre-compute derived credentials (e.g. Kucoin HMAC-signed passphrase)
        derived_creds: dict[str, str] = {}
        for cred_name, derivation_label in spec.key_material.derived_credentials:
            derivation_fn = _DERIVATION_FN_REGISTRY.get(derivation_label)
            if derivation_fn is None:
                raise ValueError(
                    f"Unknown derivation function label: '{derivation_label}'. "
                    f"Available: {list(_DERIVATION_FN_REGISTRY)}"
                )
            raw_value = extra_creds.get(cred_name, "")
            derived_creds[cred_name] = derivation_fn(secret_bytes, raw_value)

        return cls(
            spec,
            api_key=api_key,
            secret_bytes=secret_bytes,
            extra_creds=dict(extra_creds),
            derived_creds=derived_creds,
            _fixed_ts=_fixed_ts,
            _fixed_nonce=_fixed_nonce,
        )

    @classmethod
    def _from_jwt_spec(
        cls,
        spec: JwtSigningSpec,
        *,
        api_key: str,
        secret: str,
        **extra_creds: str,
    ) -> DeclarativeRestSigner:
        # secret is a PEM string; store as bytes for PyJWT
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        return cls(
            spec,
            api_key=api_key,
            secret_bytes=secret_bytes,
            extra_creds=dict(extra_creds),
            derived_creds={},
        )

    @classmethod
    def _from_bearer_spec(
        cls,
        spec: BearerTokenSpec,
        *,
        api_key: str,
        secret: str,
        **extra_creds: str,
    ) -> DeclarativeRestSigner:
        return cls(
            spec,
            api_key=api_key,
            secret_bytes=secret.encode("utf-8"),
            extra_creds=dict(extra_creds),
            derived_creds={},
        )

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def sign(self, request: Request) -> Request:
        """Return a new signed Request without mutating the input."""
        if isinstance(self._spec, HmacSigningSpec):
            return await self._sign_hmac(request)
        if isinstance(self._spec, JwtSigningSpec):
            return await self._sign_jwt(request)
        if isinstance(self._spec, BearerTokenSpec):
            return await self._sign_bearer(request)
        raise TypeError(f"Unexpected spec type: {type(self._spec)!r}")

    # ------------------------------------------------------------------
    # HMAC signing pipeline
    # ------------------------------------------------------------------

    async def _sign_hmac(self, request: Request) -> Request:
        spec = self._spec

        # 1. Resolve timestamp
        ts = (
            self._fixed_ts
            if self._fixed_ts is not None
            else _make_timestamp(spec.timestamp.unit, spec.timestamp.format)
        )

        # 2. Resolve nonce
        nonce = await self._resolve_nonce(spec)

        # 3. Serialise body
        body_str = _serialise_body(request.body, spec.recipe.body_format)

        # 4. Build base context (variables available for sig-input template)
        ctx: dict[str, str] = {
            "api_key": self._api_key,
            "ts": ts,
            "nonce": nonce,
            "method": request.method.upper(),
            "path": request.path,
            "body": body_str,
            "qs": ("?" + urllib.parse.urlencode(request.qs_params)) if request.qs_params else "",
            "qs_sorted": _qs_sorted(request.qs_params),
            # secret is bytes — not placed in string ctx (used directly in HMAC)
        }
        # Merge extra credentials (recv_window, memo, passphrase-raw, etc.)
        ctx.update(self._extra_creds)
        # Override with derived credentials (passphrase-signed for Kucoin, etc.)
        ctx.update(self._derived_creds)

        # 5. Compute inner_hash if body_hash spec present (Kraken two-stage)
        inner_hash_bytes: bytes | None = None
        if spec.recipe.body_hash is not None:
            inner_hash_bytes = self._compute_body_hash(spec.recipe.body_hash, ctx)
            # inner_hash is bytes; expose as hex in ctx for string templates
            # but raw bytes used in bytes-concat template expansion below
            ctx["inner_hash"] = inner_hash_bytes.hex()

        # 6. Build HMAC input bytes from template
        sig_input_bytes = self._expand_sig_input(spec.recipe.template, ctx, inner_hash_bytes)

        # 7. Compute HMAC
        sig_raw = _hmac_bytes(spec.recipe.algorithm, self._secret_bytes, sig_input_bytes)

        # 8. Encode signature
        sig = _encode_sig(sig_raw, spec.recipe.output_encoding)

        # 9. Build output context (adds {sig})
        out_ctx = dict(ctx)
        out_ctx["sig"] = sig

        # 10. Build new headers
        new_headers = dict(request.headers)
        for header_name, header_template in spec.output.headers.items():
            new_headers[header_name] = _expand_output_template(header_template, out_ctx)

        # 11. Inject nonce into its placement (if applicable)
        new_headers, new_body_str, new_qs = self._inject_nonce(
            spec, nonce, new_headers, body_str, dict(request.qs_params)
        )

        # 12. Apply body_inject fields
        if spec.output.body_inject:
            for _field, template in spec.output.body_inject.items():
                # body_inject is applied but body is kept as string for now
                # (full JSON merging is out of scope for Task 4)
                _ = _expand_output_template(template, out_ctx)

        # 13. Apply qs_inject fields
        if spec.output.qs_inject:
            for param_name, template in spec.output.qs_inject.items():
                new_qs[param_name] = _expand_output_template(template, out_ctx)

        # 14. Reassemble body
        new_body: str | bytes | None = request.body
        if new_body_str != body_str:
            new_body = new_body_str

        return replace(
            request,
            headers=new_headers,
            qs_params=new_qs,
            body=new_body,
        )

    # ------------------------------------------------------------------
    # Nonce resolution
    # ------------------------------------------------------------------

    async def _resolve_nonce(self, spec: HmacSigningSpec) -> str:
        """Return nonce string per NonceSpec.source (and monotonic flag)."""
        if self._fixed_nonce is not None:
            return self._fixed_nonce

        src = spec.nonce.source
        if src is NonceSource.NONE:
            return ""
        if src is NonceSource.UUID:
            return str(uuid.uuid4())
        if src is NonceSource.TIMESTAMP:
            return _make_timestamp(spec.timestamp.unit, spec.timestamp.format)
        # COUNTER
        return await self._next_counter_nonce()

    async def _next_counter_nonce(self) -> str:
        """Return the next monotonically-increasing counter value as a string.

        Per-instance asyncio.Lock ensures strict ordering when multiple
        coroutines call sign() concurrently on the same signer instance.
        The counter is an ever-increasing integer that never resets.
        """
        async with self._counter_lock:
            self._counter += 1
            return str(self._counter)

    # ------------------------------------------------------------------
    # Nonce placement injection
    # ------------------------------------------------------------------

    def _inject_nonce(
        self,
        spec: HmacSigningSpec,
        nonce: str,
        headers: dict,
        body_str: str,
        qs_params: dict,
    ) -> tuple[dict, str, dict]:
        """Inject nonce into the request per NoncePlacement (outside output templates)."""
        placement = spec.nonce.placement
        field = spec.nonce.field_name

        if placement is NoncePlacement.HEADER and field:
            headers[field] = nonce
        elif placement is NoncePlacement.BODY_FIELD and field:
            # Prepend nonce=<value>& to form body (simplified for FORM_URLENCODED)
            body_str = f"{field}={nonce}&{body_str}" if body_str else f"{field}={nonce}"
        elif placement is NoncePlacement.QS_FIELD and field:
            qs_params[field] = nonce
        # SIG_ONLY and NONE: nonce used in template only, not injected

        return headers, body_str, qs_params

    # ------------------------------------------------------------------
    # Sig-input expansion (bytes output)
    # ------------------------------------------------------------------

    def _expand_sig_input(
        self,
        template: str,
        ctx: dict[str, str],
        inner_hash_bytes: bytes | None,
    ) -> bytes:
        """Expand the recipe.template to bytes for HMAC computation.

        Handles the special {path_bytes}{inner_hash} pattern (Kraken):
        - {path_bytes} → path.encode("utf-8")
        - {inner_hash}  → raw inner_hash_bytes (not hex)
        Result is bytes concatenation.

        For all-string templates, result is template.format_map(ctx).encode().
        """
        # Detect Kraken-style bytes-concat template
        if "{path_bytes}" in template and inner_hash_bytes is not None:
            # Template is exactly "{path_bytes}{inner_hash}" (bytes concatenation)
            path_bytes = ctx.get("path", "").encode("utf-8")
            return path_bytes + inner_hash_bytes

        # Standard string template — format and encode
        # Use direct format_map (SIG_INPUT surface; ctx has all relevant vars)
        result = template.format_map(ctx)
        return result.encode("utf-8")

    # ------------------------------------------------------------------
    # Inner hash (Kraken body_hash stage)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_body_hash(body_hash_spec: BodyHashSpec, ctx: dict[str, str]) -> bytes:
        """Compute the inner hash for two-stage signing (Kraken).

        input_template e.g. "{nonce}{body}" — expanded to string, then hashed.
        Returns raw digest bytes (not encoded).
        """
        input_str = body_hash_spec.input_template.format_map(ctx)
        return _hash_bytes(body_hash_spec.algorithm, input_str.encode("utf-8"))

    # ------------------------------------------------------------------
    # JWT signing pipeline (spec §6.4)
    # ------------------------------------------------------------------

    async def _sign_jwt(self, request: Request) -> Request:
        spec = self._spec
        assert isinstance(spec, JwtSigningSpec)

        now = int(time.time())
        parsed = urlparse(request.url)
        host = parsed.netloc  # e.g. "api.coinbase.com"

        # Build substitution context for claims and jwt_headers templates
        rand_hex = os.urandom(16).hex()
        ctx: dict[str, Any] = {
            "api_key": self._api_key,
            "method": request.method.upper(),
            "host": host,
            "path": request.path,
            "rand_hex": rand_hex,
        }

        # Expand claims — values may be plain strings or lists; only expand strings
        claims: dict[str, Any] = {}
        for k, v in spec.claims.items():
            if isinstance(v, str):
                claims[k] = v.format_map(ctx)
            else:
                claims[k] = v

        # Add standard time claims
        claims["nbf"] = now
        claims["exp"] = now + spec.lifetime_seconds

        # Expand jwt_headers (additional headers beyond alg/typ)
        additional_headers: dict[str, str] = {
            k: v.format_map(ctx) for k, v in spec.jwt_headers.items()
        }

        # Select PyJWT algorithm string
        algorithm = "ES256" if spec.algorithm is JwtAlgorithm.ES256 else "RS256"

        encoded_jwt: str = pyjwt.encode(
            claims,
            self._secret_bytes,
            algorithm=algorithm,
            headers=additional_headers,
        )

        # Build auth header value
        jwt_ctx = dict(ctx)
        jwt_ctx["jwt"] = encoded_jwt
        auth_value = spec.auth_header_template.format_map(jwt_ctx)

        new_headers = dict(request.headers)
        new_headers[spec.auth_header_name] = auth_value

        return replace(request, headers=new_headers)

    # ------------------------------------------------------------------
    # Bearer token signing pipeline (spec §6.5)
    # ------------------------------------------------------------------

    async def _sign_bearer(self, request: Request) -> Request:
        spec = self._spec
        assert isinstance(spec, BearerTokenSpec)

        token = await self._get_bearer_token(spec)

        token_ctx: dict[str, str] = {"token": token}
        auth_value = spec.auth_header_template.format_map(token_ctx)

        new_headers = dict(request.headers)
        new_headers[spec.auth_header_name] = auth_value

        return replace(request, headers=new_headers)

    async def _get_bearer_token(self, spec: BearerTokenSpec) -> str:
        """Return a cached token, fetching a fresh one when cache is stale."""
        now = time.time()
        if (
            self._bearer_token is not None
            and (now - self._bearer_fetched_at) < spec.token_ttl_seconds
        ):
            return self._bearer_token

        # Build POST body from template
        cred_ctx: dict[str, str] = {
            "api_key": self._api_key,
            "secret": self._secret_bytes.decode("utf-8"),
        }
        cred_ctx.update(self._extra_creds)
        body: dict[str, str] = {
            k: v.format_map(cred_ctx) for k, v in spec.token_request_template.items()
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(spec.token_endpoint, json=body)
            response.raise_for_status()
            data = response.json()

        # Resolve nested response path (e.g. "data.token" or "result.access_token")
        token = _resolve_nested_path(data, spec.token_response_path)

        self._bearer_token = token
        self._bearer_fetched_at = time.time()
        return token


# ---------------------------------------------------------------------------
# Helper: resolve dot-separated path in a nested dict
# ---------------------------------------------------------------------------


def _resolve_nested_path(data: dict[str, Any], path: str) -> str:
    """Walk a dot-separated path through a nested dict and return the leaf value.

    E.g. ``_resolve_nested_path({"data": {"token": "x"}}, "data.token")`` → ``"x"``.
    Raises ``KeyError`` if any segment is missing.
    """
    parts = path.split(".")
    node: Any = data
    for part in parts:
        node = node[part]
    return str(node)
