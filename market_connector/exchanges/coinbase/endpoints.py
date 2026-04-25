"""Coinbase Advanced Trade REST endpoint registry with per-endpoint rate limits."""

from market_connector.transport.endpoint import Endpoint

ENDPOINT_REGISTRY: dict[str, Endpoint] = {
    # Public endpoints — limit=10/window
    "server_time": Endpoint(path="/brokerage/time", method="GET", limit=10, window=1.0),
    "products": Endpoint(path="/brokerage/market/products", method="GET", limit=10, window=1.0),
    "product_book": Endpoint(path="/brokerage/product_book", method="GET", limit=10, window=1.0),
    "candles": Endpoint(
        path="/brokerage/market/products/{product_id}/candles",
        method="GET",
        limit=10,
        window=1.0,
    ),
    # Private endpoints — limit=30/window
    "accounts": Endpoint(path="/brokerage/accounts", method="GET", limit=30, window=1.0),
    "place_order": Endpoint(path="/brokerage/orders", method="POST", limit=30, window=1.0),
    "cancel_orders": Endpoint(
        path="/brokerage/orders/batch_cancel",
        method="POST",
        limit=30,
        window=1.0,
    ),
    "list_orders": Endpoint(
        path="/brokerage/orders/historical/batch",
        method="GET",
        limit=30,
        window=1.0,
    ),
    "order_status": Endpoint(
        path="/brokerage/orders/historical/{order_id}",
        method="GET",
        limit=30,
        window=1.0,
    ),
    "order_fills": Endpoint(
        path="/brokerage/orders/historical/fills",
        method="GET",
        limit=30,
        window=1.0,
    ),
    "fee_summary": Endpoint(
        path="/brokerage/transaction_summary",
        method="GET",
        limit=30,
        window=1.0,
    ),
}
