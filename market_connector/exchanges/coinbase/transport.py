"""Context-aware REST transport for Coinbase Advanced Trade API.

RestConnectorBase's Signer protocol receives only headers — it does not pass
request context (method, path, body) to the signer hook.  Coinbase JWT/HMAC
signing requires that context, so CoinbaseRestClient overrides ``request()`` to:

1. Resolve path parameters (e.g. ``{order_id}``) from ``params``.
2. Build a full request-context dict and invoke the Coinbase signer.
3. Merge the returned auth headers and delegate to the parent with ``signer=None``
   (bypassing the parent's context-free hook).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from market_connector.transport.endpoint import Endpoint

# Signer takes full request context and returns auth headers to merge.
Signer = Callable[[dict[str, Any]], Awaitable[dict[str, str]]]


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
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(f"base_url must use https:// (got {base_url!r})")
        super().__init__(
            base_url,
            endpoints=endpoints,
            signer=None,  # framework signer bypassed; signing handled in request() below
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        self._signer = signer

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Sign and execute a rate-limited, retried request.

        Path parameters present in ``params`` (e.g. ``order_id`` for a path
        containing ``{order_id}``) are substituted into the path before
        signing and then removed from the query-string params passed upstream.
        """
        endpoint = self._endpoints[endpoint_name]

        # Resolve path params (e.g. /orders/{order_id} → /orders/abc123)
        path_params = {k: v for k, v in (params or {}).items() if "{" + k + "}" in endpoint.path}
        resolved_path = endpoint.path.format(**path_params) if path_params else endpoint.path
        query_params = {k: v for k, v in (params or {}).items() if k not in path_params}

        body_str = json.dumps(data) if data else ""
        auth_headers = await self._signer(
            {
                "context": "rest",
                "method": endpoint.method,
                "path": resolved_path,
                "body": body_str,
            }
        )

        merged = dict(headers or {})
        merged.update(auth_headers)
        return await super().request(
            endpoint_name,
            params=query_params or None,
            data=data,
            headers=merged,
        )
