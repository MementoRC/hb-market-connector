"""Rate-limited async REST client with retry and Signer-based auth injection."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from string import Formatter
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from rate_limit import RateLimit, RateLimiter

from market_connector.auth.protocols import Request, Signer
from market_connector.auth.spec import BodyFormat
from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.response import Response

if TYPE_CHECKING:
    from collections.abc import Mapping

    from market_connector.transport.endpoint import Endpoint


def _parse_retry_after(raw_response: httpx.Response) -> float | None:
    """Parse a Retry-After header given in seconds; None if missing or unparsable.

    The HTTP-date form is intentionally ignored (treated as unparsable).
    """
    value = raw_response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def _decode_body(raw_response: httpx.Response) -> dict | list | None:
    """Safely decode a JSON body. Returns None for empty (204) or non-JSON responses.

    Content-encoding (gzip, deflate, br) is handled transparently by httpx
    before this function is called — `raw_response.content` is already the
    decompressed bytes.

    Strict media-type matching: `application/json` only. Variants like
    `application/problem+json` (RFC 7807) and `application/json-patch+json`
    are intentionally NOT parsed by default — extend this function or add a
    per-endpoint decoder if/when a connector needs them.
    """
    if not raw_response.content:
        return None
    # Strip parameters (`; charset=utf-8`) before exact match.
    base_type = raw_response.headers.get("content-type", "").split(";")[0].strip().lower()
    if base_type != "application/json":
        return None
    return raw_response.json()  # type: ignore[no-any-return]


def _is_transient(raw_response: httpx.Response) -> bool:
    """5xx, or a non-JSON HTML body (Cloudflare block/challenge page) of any status."""
    if raw_response.status_code >= 500:
        return True
    if not raw_response.content:
        return False
    base_type = raw_response.headers.get("content-type", "").split(";")[0].strip().lower()
    if base_type == "application/json":
        return False
    return base_type == "text/html" or raw_response.content.lstrip().startswith(b"<")


class RestConnectorBase:
    """Rate-limited REST client with retry and optional Signer-based auth.

    Args:
        base_url: Base URL for the exchange API.
        endpoints: Mapping of endpoint name -> Endpoint config.
        signer: Signer Protocol instance that signs a Request envelope.
        max_retries: Number of retries on transient (5xx) errors.
        retry_delay: Initial delay between retries in seconds (doubles each retry).
        rate_limiter: hb-rate-limit ``RateLimiter`` shared across clients; a private
            one is created when omitted. An endpoint whose name is already a registered
            limit (e.g. from ``limiter_from_flat_spec``) is charged under that name;
            any other endpoint is registered lazily as ``"{METHOD}:{path}"``.

    Rate limiting waits: when an endpoint's limit is exhausted (or paused after a
    429), ``request`` sleeps until capacity is available instead of raising
    ``RateLimitError``. ``RateLimitError`` is raised only for an exchange-side 429.
    """

    def __init__(
        self,
        base_url: str,
        endpoints: dict[str, Endpoint] | None = None,
        signer: Signer | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoints = endpoints or {}
        self._signer = signer
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient()
        self._rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        self._registered_limits: set[str] = set()

    @property
    def rate_limiter(self) -> RateLimiter:
        """The hb-rate-limit ``RateLimiter`` charged by this client."""
        return self._rate_limiter

    def _register_limit(self, endpoint_name: str, endpoint: Endpoint) -> str:
        """Return the endpoint's limit_id, registering a METHOD:path limit lazily.

        A limit already registered under the endpoint name (e.g. from a pool spec)
        is used as-is; otherwise a per-endpoint ``"{METHOD}:{path}"`` limit is added.
        """
        if self._rate_limiter.has_limit(endpoint_name):
            return endpoint_name
        limit_id = f"{endpoint.method}:{endpoint.path}"
        if limit_id not in self._registered_limits:
            self._rate_limiter.add_limit(RateLimit(limit_id, endpoint.limit, endpoint.window))
            self._registered_limits.add(limit_id)
        return limit_id

    def _resolve_path(self, endpoint: Endpoint, path_params: Mapping[str, Any] | None) -> str:
        """Substitute ``{name}`` placeholders in the endpoint path with URL-quoted values.

        Raises ValueError if a placeholder has no value or a key is not a placeholder.
        """
        values = path_params or {}
        names = {name for _, name, _, _ in Formatter().parse(endpoint.path) if name}
        missing = sorted(names - values.keys())
        unexpected = sorted(values.keys() - names)
        if missing or unexpected:
            raise ValueError(
                f"path params for {endpoint.path!r}: missing {missing}, unexpected {unexpected}"
            )
        if not names:
            return endpoint.path
        return endpoint.path.format_map({k: quote(str(v), safe="") for k, v in values.items()})

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        path_params: Mapping[str, Any] | None = None,
        limit_id: str | None = None,
        weights: Mapping[str, int] | None = None,
        pools: Mapping[str, int] | None = None,
        signer: Signer | None = None,
    ) -> Response:
        """Execute a rate-limited, retried request.

        Returns a typed Response wrapper with `raw` body decoded by `_decode_body`,
        `status_code`, `headers`, `_endpoint` name, and `_response_type` from the
        endpoint registry.  Call `.parse()` on the result to obtain a validated
        Pydantic model when `response_type` is configured on the endpoint.

        Rate limiting: the endpoint's weight is reserved before signing and this
        call waits (does not raise) while the limit is exhausted or paused. The
        charge is refunded only if the signer raises (nothing was sent); nothing
        is refunded once the request is sent, and 5xx retries do not re-charge.
        A 429 reports ``limiter.report_throttled`` (with Retry-After seconds when
        present) and raises ``RateLimitError``.

        Templated paths: ``path_params`` fills ``{name}`` placeholders (values are
        URL-quoted) before anything is charged; a mismatch raises ``ValueError``
        and nothing is sent. The limit id stays keyed on the template path.

        ``limit_id`` overrides the limit charged (and throttled on a 429), e.g. a
        per-key limit from ``ensure_keyed_limit``; it must already be registered,
        otherwise ``ValueError`` is raised before anything is charged or sent.

        ``weights`` replaces the default ``{limit_id: endpoint.weight}`` charge, e.g. to
        bill a linked pool a per-call amount (Kraken age penalties). Its keys must be
        the limit or its linked limits; hb-rate-limit raises ``ValueError`` otherwise,
        before anything is sent.

        ``pools`` is hb-rate-limit's per-call ``{pool_id: cost}`` override: it replaces a
        linked pool's declared charge (0 skips that pool; the endpoint itself is still
        charged) or charges another registered pool ad hoc, registering nothing.

        ``signer`` overrides the configured signer for this call. Only then is
        ``data`` serialized once to a JSON string that is both signed (as
        ``Request.body``) and sent verbatim, so the signed and sent bodies match.
        """
        endpoint = self._endpoints[endpoint_name]
        resolved_path = self._resolve_path(endpoint, path_params)
        if limit_id is None:
            limit_id = self._register_limit(endpoint_name, endpoint)
        elif not self._rate_limiter.has_limit(limit_id):
            raise ValueError(f"limit_id {limit_id!r} is not registered (missing)")
        charge = await self._rate_limiter.reserve(
            limit_id,
            weights if weights is not None else {limit_id: endpoint.weight},
            pools=pools,
        )

        url = f"{self._base_url}{resolved_path}"
        active_signer = signer if signer is not None else self._signer
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            # Built and signed per attempt: a retry needs a fresh nonce and signature.
            try:
                req, content, json_body, send_headers = await self._build_call(
                    endpoint, url, params, data, headers, active_signer
                )
            except Exception:
                if attempt == 0:
                    charge.refund()  # nothing was sent
                raise
            try:
                response = await self._client.request(
                    method=req.method,
                    url=url,
                    params=req.qs_params or None,
                    json=json_body,
                    content=content,
                    headers=send_headers,
                )
            except httpx.TransportError as exc:
                if not endpoint.idempotent:
                    raise
                last_error = ExchangeUnavailableError(f"transport error on {endpoint_name}: {exc}")
                last_error.__cause__ = exc
            else:
                if response.status_code == 401:
                    raise AuthenticationError(f"401 from {endpoint_name}: {response.text}")

                if response.status_code == 429:
                    self._rate_limiter.report_throttled(limit_id, _parse_retry_after(response))
                    raise RateLimitError(
                        f"429 from {endpoint_name}: exchange-side rate limit",
                        retry_after=_parse_retry_after(response),
                    )

                if not _is_transient(response):
                    return Response(
                        raw=_decode_body(response),
                        status_code=response.status_code,
                        headers=response.headers,
                        _endpoint=endpoint_name,
                        _response_type=endpoint.response_type,
                    )
                last_error = ExchangeUnavailableError(
                    f"{response.status_code} from {endpoint_name}: {response.text[:200]}"
                )
                if not endpoint.idempotent:
                    raise last_error  # never re-send an order-placing call blindly

            if attempt < self._max_retries:
                await asyncio.sleep(self._retry_delay * (2**attempt))

        raise last_error or ExchangeUnavailableError("request failed")

    async def _build_call(
        self,
        endpoint: Endpoint,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        headers: dict[str, str] | None,
        active_signer: Signer | None,
    ) -> tuple[Request, str | None, dict[str, Any] | None, dict[str, str]]:
        """Build (and sign, when a signer is active) one send attempt."""
        req = Request(
            method=endpoint.method,
            url=url,
            path=urlparse(url).path,
            headers=dict(headers or {}),
            body=None,
            qs_params=dict(params or {}),
        )
        if active_signer is None:
            return req, None, data, req.headers
        content_type = "application/json"
        body_format = getattr(active_signer, "body_format", BodyFormat.JSON)
        is_body_call = endpoint.method.upper() != "GET"
        if is_body_call and body_format is BodyFormat.FORM_URLENCODED:
            req = replace(req, body=urlencode(data or {}))
            content_type = "application/x-www-form-urlencoded"
        elif is_body_call and data:
            req = replace(req, body=json.dumps(data))
        req = await active_signer.sign(req)
        if is_body_call and req.body is not None:
            content = req.body if isinstance(req.body, str) else req.body.decode("utf-8")
            return req, content, None, {"Content-Type": content_type, **req.headers}
        return req, None, None, req.headers

    async def close(self) -> None:
        await self._client.aclose()
