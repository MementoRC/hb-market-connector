"""Kraken REST endpoint registry.

Rate-limit pool assignments reference the ``rate_limit_pool`` keys declared in
``market_connector.exchanges.kraken.specs.KRAKEN_RATE_LIMIT_SPEC``:
  - ``"public"``   — unauthenticated market-data endpoints
  - ``"private"``  — authenticated account / order-query endpoints
  - ``"matching"`` — trading endpoints (AddOrder, CancelOrder); these also
                     charge the ``"private"`` counter per Kraken's published
                     rate-limit documentation.  The dual-charging metadata
                     lives in the spec's ``endpoint_pools`` field; the
                     Endpoint dataclass carries only a single ``rate_limit_pool``
                     field (the *primary* pool) so we use ``"matching"`` here.

All private endpoints accept POST with a form-encoded body (BodyFormat.FORM_URLENCODED
is set in Stage 1 KRAKEN_HMAC_SPEC; the transport layer encodes automatically).
"""

from market_connector.transport.endpoint import Endpoint

ENDPOINT_REGISTRY: dict[str, Endpoint] = {
    # ------------------------------------------------------------------
    # Public endpoints  (rate_limit_pool="public")
    # ------------------------------------------------------------------
    "asset_pairs": Endpoint(path="/0/public/AssetPairs", method="GET"),
    "ticker": Endpoint(path="/0/public/Ticker", method="GET"),
    "depth": Endpoint(path="/0/public/Depth", method="GET"),
    "server_time": Endpoint(path="/0/public/Time", method="GET"),
    # ------------------------------------------------------------------
    # Private endpoints  (rate_limit_pool="private")
    # ------------------------------------------------------------------
    "balance": Endpoint(path="/0/private/Balance", method="POST"),
    "get_websockets_token": Endpoint(
        path="/0/private/GetWebSocketsToken",
        method="POST",
    ),
    "open_orders": Endpoint(path="/0/private/OpenOrders", method="POST"),
    "query_orders": Endpoint(path="/0/private/QueryOrders", method="POST"),
    "query_trades": Endpoint(path="/0/private/QueryTrades", method="POST"),
    # ------------------------------------------------------------------
    # Matching / trading endpoints  (rate_limit_pool="matching")
    # ------------------------------------------------------------------
    "add_order": Endpoint(path="/0/private/AddOrder", method="POST"),
    "cancel_order": Endpoint(path="/0/private/CancelOrder", method="POST"),
}
