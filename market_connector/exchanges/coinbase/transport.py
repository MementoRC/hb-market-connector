"""Context-aware REST transport for Coinbase Advanced Trade API.

RestConnectorBase's base signer receives a Request and returns a signed Request.
CoinbaseRestClient overrides ``request()`` to:

1. Resolve path parameters (e.g. ``{order_id}``) from ``params``.
2. Build a Request envelope and invoke the Coinbase framework Signer.
3. Merge the returned auth headers and delegate to the parent with ``signer=None``
   (bypassing the parent's default signing hook).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from market_connector.auth.protocols import Request, Signer
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
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
        self._coinbase_signer = signer

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
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

        return await super().request(
            endpoint_name,
            params=query_params or None,
            data=data,
            headers=dict(signed_req.headers),
        )
