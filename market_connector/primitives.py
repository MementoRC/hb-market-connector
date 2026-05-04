"""Frozen Pydantic v2 domain primitives for the exchange gateway framework.

All models are immutable (frozen=True). Connectors convert exchange-specific
schemas to these types in their converters.py module.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TCH003
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# Trading-pair string as used throughout the exchange gateway (e.g. "BTC-USDT").
# A type alias of str keeps existing code unchanged while giving the type checker
# a named concept to anchor against InstrumentRef (the structured peer).
type ConnectorPair = str


class _StrValue(StrEnum):
    """Base mixin: str(member) returns the value."""


class OrderType(_StrValue):
    """Order type for gateway execution methods."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class TradeType(_StrValue):
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
