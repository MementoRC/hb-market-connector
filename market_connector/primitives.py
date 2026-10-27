"""Frozen Pydantic v2 domain primitives for the exchange gateway framework.

All models are immutable (frozen=True). Connectors convert exchange-specific
schemas to these types in their converters.py module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TCH003

from data_type_primitives import PositionSide  # noqa: TCH002
from pydantic import BaseModel, ConfigDict

from market_connector.hb_compat.primitives import OrderType, TradeType

# Trading-pair string as used throughout the exchange gateway (e.g. "BTC-USDT").
# A type alias of str keeps existing code unchanged while giving the type checker
# a named concept to anchor against InstrumentRef (the structured peer).
type ConnectorPair = str

__all__ = [
    "CancellationResult",
    "CandleData",
    "ConnectorPair",
    "FundingInfo",
    "FundingInfoUpdate",
    "OpenOrder",
    "OrderBookSnapshot",
    "OrderBookUpdate",
    "OrderType",
    "Position",
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


class FundingInfoUpdate(BaseModel):
    """Partial funding update; ``None`` fields mean "unchanged".

    Mirrors hummingbot's ``FundingInfoUpdate``. The legacy mutable API lives in
    ``market_connector.hb_compat.funding_info``.
    """

    model_config = ConfigDict(frozen=True)
    trading_pair: str
    index_price: Decimal | None = None
    mark_price: Decimal | None = None
    next_funding_utc_timestamp: int | None = None
    rate: Decimal | None = None


class FundingInfo(BaseModel):
    """Funding snapshot of a perpetual market.

    Mirrors hummingbot's ``FundingInfo``. The legacy mutable API lives in
    ``market_connector.hb_compat.funding_info``.
    """

    model_config = ConfigDict(frozen=True)
    trading_pair: str
    index_price: Decimal
    mark_price: Decimal
    next_funding_utc_timestamp: int
    rate: Decimal

    def with_update(self, update: FundingInfoUpdate) -> FundingInfo:
        """Return a new ``FundingInfo`` with the update's non-None fields applied.

        Raises:
            ValueError: If ``update`` targets a different trading pair.
        """
        if update.trading_pair != self.trading_pair:
            raise ValueError(
                f"FundingInfoUpdate for {update.trading_pair!r} cannot update {self.trading_pair!r}"
            )
        changes = update.model_dump(exclude={"trading_pair"}, exclude_none=True)
        return self.model_copy(update=changes)


class CancellationResult(BaseModel):
    """Outcome of a single order cancellation.

    Mirrors hummingbot's ``CancellationResult``. The legacy tuple form lives in
    ``market_connector.hb_compat.cancellation_result``.
    """

    model_config = ConfigDict(frozen=True)
    order_id: str
    success: bool


class Position(BaseModel):
    """An open derivative position.

    Mirrors hummingbot's ``Position``. The legacy mutable API lives in
    ``market_connector.hb_compat.position``.
    """

    model_config = ConfigDict(frozen=True)
    trading_pair: str
    position_side: PositionSide
    unrealized_pnl: Decimal
    entry_price: Decimal
    amount: Decimal
    leverage: Decimal
