"""Frozen Pydantic v2 domain primitives for the exchange gateway framework.

All models are immutable (frozen=True). Connectors convert exchange-specific
schemas to these types in their converters.py module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TCH003

from pydantic import BaseModel, ConfigDict

from market_connector.hb_compat.primitives import OrderType, TradeType

# Trading-pair string as used throughout the exchange gateway (e.g. "BTC-USDT").
# A type alias of str keeps existing code unchanged while giving the type checker
# a named concept to anchor against InstrumentRef (the structured peer).
type ConnectorPair = str

__all__ = [
    "CandleData",
    "ConnectorPair",
    "OpenOrder",
    "OrderBookSnapshot",
    "OrderBookUpdate",
    "OrderType",
    "TradeEvent",
    "TradeType",
]


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


@dataclass(frozen=True, slots=True)
class CandleData:
    """Single OHLCV candle bar. Frozen and slot-based for immutability and memory efficiency."""

    trading_pair: str
    timestamp: float  # bar open time, epoch seconds
    interval: str  # canonical interval label e.g. "1m", "5m", "1h", "1d"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # 0 if exchange reports no volume for the bar
