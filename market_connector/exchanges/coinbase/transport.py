"""Context-aware REST transport for Coinbase Advanced Trade API.

RestConnectorBase's base signer receives a Request and returns a signed Request.
CoinbaseRestClient overrides ``request()`` to:

1. Split path parameters (e.g. ``{order_id}``) from query params in ``params``.
2. Delegate to the parent with the Coinbase Signer as the per-call ``signer`` and
   ``path_params``: the base signs after the rate-limit reserve (so the short-lived
   JWT cannot expire while waiting), signing the resolved path and the exact body
   string it sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rate_limit import RateLimiter

    from market_connector.auth.protocols import Signer
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
        weights: Mapping[str, int] | None = None,
        pools: Mapping[str, int] | None = None,
        signer: Signer | None = None,
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

        extra: dict[str, Any] = {} if limit_id is None else {"limit_id": limit_id}
        return await super().request(
            endpoint_name,
            params=query_params or None,
            data=data,
            headers=headers,
            path_params=path_params,
            weights=weights,
            pools=pools,
            signer=signer if signer is not None else self._coinbase_signer,
            **extra,
        )
