"""Coinbase Advanced Trade API WebSocket message schema models."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class _FrozenBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ---------------------------------------------------------------------------
# Level 2 order book
# ---------------------------------------------------------------------------


class Level2Update(_FrozenBase):
    side: str  # "bid" or "offer"
    event_time: str
    price_level: str
    new_quantity: str


class Level2Event(_FrozenBase):
    type: str  # "snapshot" or "update"
    product_id: str
    updates: list[Level2Update]


# ---------------------------------------------------------------------------
# Market trades
# ---------------------------------------------------------------------------


class MarketTrade(_FrozenBase):
    trade_id: str
    product_id: str
    price: str
    size: str
    side: str
    time: str


class MarketTradesEvent(_FrozenBase):
    type: str
    trades: list[MarketTrade]


# ---------------------------------------------------------------------------
# User orders
# ---------------------------------------------------------------------------


class UserOrder(_FrozenBase):
    order_id: str
    client_order_id: str
    product_id: str
    cumulative_quantity: str
    leaves_quantity: str
    avg_price: str
    total_fees: str
    status: str
    creation_time: str
    order_side: str
    order_type: str | None = None


class UserEvent(_FrozenBase):
    type: str
    orders: list[UserOrder]


# ---------------------------------------------------------------------------
# Top-level envelope
# ---------------------------------------------------------------------------


class WsMessage(_FrozenBase):
    channel: str
    client_id: str = ""
    timestamp: str
    sequence_num: int = 0
    events: list[dict[str, Any]]  # Raw — parsed per-channel by dispatcher
