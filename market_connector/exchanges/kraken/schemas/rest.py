"""Kraken REST API response schema models (Pydantic v2).

All Kraken REST endpoints return the same top-level envelope::

    {"error": [...], "result": T}

``KrakenResponse[T]`` models this envelope generically.  Concrete result models
are defined below and validated against the Stage 2 recorded fixtures.

Pydantic v2 conventions used throughout:
- ``model_config = ConfigDict(...)`` (not ``class Config:``)
- ``@field_validator(..., mode="before")`` + ``@classmethod`` for coercions
- ``Field(..., validation_alias=...)`` for non-Python key names
- ``extra="ignore"`` on all models (Kraken returns many undocumented fields)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ---------------------------------------------------------------------------
# Generic response envelope
# ---------------------------------------------------------------------------


class KrakenResponse[T](BaseModel):
    """Generic Kraken REST response envelope.

    Every Kraken REST endpoint returns ``{"error": [...], "result": T}``.
    ``error`` is always a list (empty on success).  ``result`` is ``None``
    when the error list is non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    error: list[str] = Field(default_factory=list)
    result: T | None = None


# ---------------------------------------------------------------------------
# /0/public/Time
# ---------------------------------------------------------------------------


class TimeResult(_Base):
    unixtime: int
    rfc1123: str


# ---------------------------------------------------------------------------
# /0/public/AssetPairs
# ---------------------------------------------------------------------------


class AssetPairInfo(_Base):
    altname: str = ""
    wsname: str = ""
    base: str = ""
    quote: str = ""
    pair_decimals: int = 0
    lot_decimals: int = 8
    ordermin: str = "0"


# AssetPairs result is a dict mapping pair name → AssetPairInfo
AssetPairsResult = dict[str, AssetPairInfo]


# ---------------------------------------------------------------------------
# /0/public/Ticker
# ---------------------------------------------------------------------------


class TickerValues(_Base):
    """A single ticker field that Kraken returns as a [value, volume_weighted_avg] pair."""

    price: str
    whole_lot_volume: str = ""
    lot_volume: str = ""

    @field_validator("price", mode="before")
    @classmethod
    def extract_first(cls, v: Any) -> Any:
        """Kraken returns many ticker fields as [price, lot_volume] arrays."""
        if isinstance(v, list) and v:
            return v[0]
        return v


class TickerInfo(_Base):
    """Ticker info for a single trading pair.

    Uses ``extra="ignore"`` to silently drop the many extra fields Kraken
    returns (``h``, ``l``, ``o``, ``t``, ``v``, etc.) that are not needed
    for the core schema validation tests.
    """

    # Ask: [price, whole_lot_volume, lot_volume]
    a: list[str] = Field(default_factory=list)
    # Bid: [price, whole_lot_volume, lot_volume]
    b: list[str] = Field(default_factory=list)
    # Last trade closed: [price, lot_volume]
    c: list[str] = Field(default_factory=list)


# Ticker result is a dict mapping pair name → TickerInfo
TickerResult = dict[str, TickerInfo]


# ---------------------------------------------------------------------------
# /0/public/Depth
# ---------------------------------------------------------------------------


class DepthResult(_Base):
    """Order-book depth result for a single pair."""

    # Each entry is [price, volume, timestamp]
    asks: list[list[str]] = Field(default_factory=list)
    bids: list[list[str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /0/private/Balance
# ---------------------------------------------------------------------------

# Balance result: {asset_code: balance_str}
BalanceResult = dict[str, str]


# ---------------------------------------------------------------------------
# /0/private/GetWebSocketsToken
# ---------------------------------------------------------------------------


class WebSocketsTokenResult(_Base):
    token: str
    expires: int


# ---------------------------------------------------------------------------
# /0/private/AddOrder
# ---------------------------------------------------------------------------


class AddOrderDescr(_Base):
    order: str = ""
    close: str | None = None


class AddOrderResult(_Base):
    txid: list[str] = Field(default_factory=list)
    descr: AddOrderDescr


# ---------------------------------------------------------------------------
# /0/private/CancelOrder
# ---------------------------------------------------------------------------


class CancelOrderResult(_Base):
    count: int


# ---------------------------------------------------------------------------
# /0/private/OpenOrders  /  QueryOrders
# ---------------------------------------------------------------------------


class OrderDescription(_Base):
    pair: str = ""
    type: str = ""  # buy / sell
    ordertype: str = ""
    price: str = "0"
    price2: str = "0"
    leverage: str = "none"
    order: str = ""


class OrderInfo(_Base):
    refid: str | None = None
    userref: int | None = None
    status: str = ""
    opentm: float = 0.0
    starttm: float = 0.0
    expiretm: float = 0.0
    descr: OrderDescription
    vol: str = "0"
    vol_exec: str = "0"
    cost: str = "0"
    fee: str = "0"
    price: str = "0"
    misc: str = ""


# OpenOrders / QueryOrders result: {order_id: OrderInfo}
OpenOrdersResult = dict[str, OrderInfo]
QueryOrdersResult = dict[str, OrderInfo]


# ---------------------------------------------------------------------------
# /0/private/QueryTrades
# ---------------------------------------------------------------------------


class TradeInfo(_Base):
    ordertxid: str = ""
    pair: str = ""
    time: float = 0.0
    type: str = ""
    ordertype: str = ""
    price: str = "0"
    cost: str = "0"
    fee: str = "0"
    vol: str = "0"
    margin: str = "0"


# QueryTrades result: {trade_id: TradeInfo}
QueryTradesResult = dict[str, TradeInfo]
