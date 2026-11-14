"""Plain-dataclass order events mirroring hummingbot's ``MarketEvent`` payloads.

hummingbot's own event classes live in Cython/``hummingbot.core`` and cannot be
imported here (ADR 0001), so these dataclasses reproduce their field names,
declaration order and defaults. A connector core publishes them on an
:class:`~market_connector.hb_compat.event_bus.EventBus` under the integer ids in
:data:`MARKET_EVENT_IDS` (hummingbot's ``MarketEvent`` values), so a host can
subscribe a bridge listener per id.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - dataclass field annotations
from typing import Any

from data_type_primitives.common import OrderType, PositionAction, TradeType

__all__ = [
    "MARKET_EVENT_IDS",
    "BuyOrderCompletedEvent",
    "BuyOrderCreatedEvent",
    "MarketOrderFailureEvent",
    "OrderCancelledEvent",
    "OrderFilledEvent",
    "SellOrderCompletedEvent",
    "SellOrderCreatedEvent",
]

_NIL = PositionAction.NIL.value


@dataclass
class BuyOrderCompletedEvent:
    timestamp: float
    order_id: str
    base_asset: str
    quote_asset: str
    base_asset_amount: Decimal
    quote_asset_amount: Decimal
    order_type: OrderType
    exchange_order_id: str | None = None


@dataclass
class SellOrderCompletedEvent:
    timestamp: float
    order_id: str
    base_asset: str
    quote_asset: str
    base_asset_amount: Decimal
    quote_asset_amount: Decimal
    order_type: OrderType
    exchange_order_id: str | None = None


@dataclass
class OrderCancelledEvent:
    timestamp: float
    order_id: str
    exchange_order_id: str | None = None


@dataclass
class OrderFilledEvent:
    timestamp: float
    order_id: str
    trading_pair: str
    trade_type: TradeType
    order_type: OrderType
    price: Decimal
    amount: Decimal
    trade_fee: Any
    exchange_trade_id: str = ""
    exchange_order_id: str = ""
    leverage: int = 1
    position: str = _NIL


@dataclass
class MarketOrderFailureEvent:
    timestamp: float
    order_id: str
    order_type: OrderType
    error_message: str | None = None
    error_type: str | None = None


@dataclass
class BuyOrderCreatedEvent:
    timestamp: float
    type: OrderType
    trading_pair: str
    amount: Decimal
    price: Decimal
    order_id: str
    creation_timestamp: float
    exchange_order_id: str | None = None
    leverage: int = 1
    position: str = _NIL


@dataclass
class SellOrderCreatedEvent:
    timestamp: float
    type: OrderType
    trading_pair: str
    amount: Decimal
    price: Decimal
    order_id: str
    creation_timestamp: float
    exchange_order_id: str | None = None
    leverage: int = 1
    position: str = _NIL


# hummingbot ``MarketEvent`` integer values.
MARKET_EVENT_IDS: dict[str, int] = {
    "BuyOrderCompletedEvent": 102,
    "SellOrderCompletedEvent": 103,
    "OrderCancelledEvent": 106,
    "OrderFilledEvent": 107,
    "MarketOrderFailureEvent": 198,
    "BuyOrderCreatedEvent": 200,
    "SellOrderCreatedEvent": 201,
}
