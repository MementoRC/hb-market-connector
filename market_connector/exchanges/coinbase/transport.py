"""Context-aware REST transport for Coinbase Advanced Trade API.

RestConnectorBase's base signer receives a Request and returns a signed Request.
CoinbaseRestClient overrides ``request()`` to:

1. Split path parameters (e.g. ``{order_id}``) from query params in ``params``.
2. Resolve the path via the base and sign a Request envelope with the Coinbase Signer.
3. Merge the returned auth headers and delegate to the parent with ``signer=None``
   (bypassing the parent's default signing hook), passing ``path_params`` so the
   base resolves the same path: the signed path equals the sent path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, override

from market_connector.auth.protocols import Request, Signer
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rate_limit import RateLimiter

    from market_connector.transport.endpoint import Endpoint
    from market_connector.transport.response import Response


class CoinbaseRestClient(RestConnectorBase):
    """RestConnectorBase subclass that signs requests with method+path+body context."""

    def __init__(
        self,
        *,
        base_url: str,
        endpoints: dict[str, Endpoint],
        signer: Signer,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(f"base_url must use https:// (got {base_url!r})")
        super().__init__(
            base_url,
            endpoints=endpoints,
            signer=None,  # framework signer bypassed; signing handled in request() below
            max_retries=max_retries,
            retry_delay=retry_delay,
            rate_limiter=rate_limiter,
        )
        self._coinbase_signer = signer

    @override
    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        path_params: Mapping[str, Any] | None = None,
        limit_id: str | None = None,
    ) -> Response:
        """Sign and execute a rate-limited, retried request.

        Path parameters present in ``params`` (e.g. ``order_id`` for a path
        containing ``{order_id}``) are substituted into the path before
        signing and then removed from the query-string params passed upstream.
        """
        endpoint = self._endpoints[endpoint_name]

        # Split path params (e.g. /orders/{order_id}) from query params
        split = {k: v for k, v in (params or {}).items() if "{" + k + "}" in endpoint.path}
        query_params = {k: v for k, v in (params or {}).items() if k not in split}
        path_params = {**split, **(path_params or {})}
        resolved_path = self._resolve_path(endpoint, path_params)

        body_str = json.dumps(data) if data else ""
        url = f"{self._base_url}{resolved_path}"
        req = Request(
            method=endpoint.method,
            url=url,
            path=resolved_path,
            headers=dict(headers or {}),
            body=body_str or None,
            qs_params=dict(query_params),
        )
        signed_req = await self._coinbase_signer.sign(req)

        extra: dict[str, Any] = {} if limit_id is None else {"limit_id": limit_id}
        return await super().request(
            endpoint_name,
            params=query_params or None,
            data=data,
            headers=dict(signed_req.headers),
            path_params=path_params,
            **extra,
        )
