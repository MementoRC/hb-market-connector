"""Frozen Pydantic v2 domain primitives for the exchange gateway framework.

All models are immutable (frozen=True). Connectors convert exchange-specific
schemas to these types in their converters.py module.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TCH003
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class OrderType(StrEnum):
    """Order type for gateway execution methods."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class TradeType(StrEnum):
    """Trade side for gateway execution methods."""

    BUY = "BUY"
    SELL = "SELL"


class OpenOrder(BaseModel):
    """An open order as reported by the exchange."""

    model_config = ConfigDict(frozen=True)
    client_order_id: str
    exchange_order_id: str
    trading_pair: str
    order_type: OrderType
    side: TradeType
    amount: Decimal
    price: Decimal
    filled_amount: Decimal
    status: str


class TradeEvent(BaseModel):
    """A single trade (fill or public trade)."""

    model_config = ConfigDict(frozen=True)
    exchange_trade_id: str
    trading_pair: str
    price: Decimal
    amount: Decimal
    side: TradeType
    timestamp: float


class OrderBookSnapshot(BaseModel):
    """Full order book from a REST endpoint."""

    model_config = ConfigDict(frozen=True)
    trading_pair: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    timestamp: float


class OrderBookUpdate(BaseModel):
    """Incremental order book delta from a WebSocket stream."""

    model_config = ConfigDict(frozen=True)
    trading_pair: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    update_id: int
