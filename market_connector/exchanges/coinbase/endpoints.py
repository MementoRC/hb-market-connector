"""Coinbase Advanced Trade REST endpoint registry.

Rate-limit metadata (pool assignment, cost) lives in
``market_connector.exchanges.coinbase.specs.COINBASE_RATE_LIMIT_SPEC``.
Endpoints carry URL + method only.
"""

from market_connector.transport.endpoint import Endpoint

ENDPOINT_REGISTRY: dict[str, Endpoint] = {
    # Public endpoints
    "server_time": Endpoint(path="/brokerage/time", method="GET"),
    "products": Endpoint(path="/brokerage/market/products", method="GET"),
    "product_book": Endpoint(path="/brokerage/product_book", method="GET"),
    "candles": Endpoint(
        path="/brokerage/market/products/{product_id}/candles",
        method="GET",
    ),
    # Private endpoints
    "accounts": Endpoint(path="/brokerage/accounts", method="GET"),
    "place_order": Endpoint(path="/brokerage/orders", method="POST"),
    "cancel_orders": Endpoint(
        path="/brokerage/orders/batch_cancel",
        method="POST",
    ),
    "list_orders": Endpoint(
        path="/brokerage/orders/historical/batch",
        method="GET",
    ),
    "order_status": Endpoint(
        path="/brokerage/orders/historical/{order_id}",
        method="GET",
    ),
    "order_fills": Endpoint(
        path="/brokerage/orders/historical/fills",
        method="GET",
    ),
    "fee_summary": Endpoint(
        path="/brokerage/transaction_summary",
        method="GET",
    ),
}
