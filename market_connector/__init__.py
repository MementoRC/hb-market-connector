"""hb-market-connector: Exchange Gateway Framework.

Public API:
    - Protocols: ExchangeGateway, ExecutionGateway, MarketDataGateway
    - Primitives: OpenOrder, TradeEvent, OrderBookSnapshot, OrderBookUpdate,
                  OrderType, TradeType
    - Exceptions: GatewayError and subclasses
"""

from market_connector.__about__ import __version__
from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    GatewayError,
    GatewayNotStartedError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)
from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)
from market_connector.protocols import ExchangeGateway, ExecutionGateway, MarketDataGateway

__all__ = [
    "__version__",
    # Protocols
    "ExchangeGateway",
    "ExecutionGateway",
    "MarketDataGateway",
    # Primitives
    "OpenOrder",
    "OrderBookSnapshot",
    "OrderBookUpdate",
    "OrderType",
    "TradeEvent",
    "TradeType",
    # Exceptions
    "AuthenticationError",
    "ExchangeUnavailableError",
    "GatewayError",
    "GatewayNotStartedError",
    "OrderNotFoundError",
    "OrderRejectedError",
    "RateLimitError",
    "SubscriptionLimitError",
]
