"""Coinbase Advanced Trade API REST response schema models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from market_connector.exchanges.coinbase.schemas.enums import (
    CoinbaseOrderSide,
    CoinbaseOrderStatus,
    CoinbaseOrderType,
    CoinbaseTimeInForce,
)


class _FrozenBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class Balance(_FrozenBase):
    value: str
    currency: str


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class Account(_FrozenBase):
    uuid: str
    name: str
    currency: str
    available_balance: Balance
    hold: Balance
    default: bool = False
    active: bool = True
    ready: bool = True
    type: str | None = None


class ListAccountsResponse(_FrozenBase):
    accounts: list[Account]
    has_next: bool = False
    cursor: str | None = None
    size: int = 0


# ---------------------------------------------------------------------------
# Order configuration sub-models
# ---------------------------------------------------------------------------


class MarketIOCConfig(_FrozenBase):
    quote_size: str | None = None
    base_size: str | None = None


class LimitGTCConfig(_FrozenBase):
    base_size: str
    limit_price: str
    post_only: bool = False


class LimitGTDConfig(LimitGTCConfig):
    end_time: datetime


class StopLimitGTCConfig(_FrozenBase):
    base_size: str
    limit_price: str
    stop_price: str
    stop_direction: str


class OrderConfiguration(_FrozenBase):
    market_market_ioc: MarketIOCConfig | None = None
    limit_limit_gtc: LimitGTCConfig | None = None
    limit_limit_gtd: LimitGTDConfig | None = None
    stop_limit_stop_limit_gtc: StopLimitGTCConfig | None = None


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


class Order(_FrozenBase):
    order_id: str
    client_order_id: str
    product_id: str
    user_id: str | None = None
    order_configuration: OrderConfiguration | None = None
    side: CoinbaseOrderSide
    status: CoinbaseOrderStatus
    time_in_force: CoinbaseTimeInForce | None = None
    created_time: datetime | None = None
    filled_size: str = "0"
    average_filled_price: str = "0"
    total_fees: str = "0"
    filled_value: str = "0"
    completion_percentage: str = "0"
    number_of_fills: str = "0"
    pending_cancel: bool = False
    settled: bool = False
    reject_reason: str | None = None
    order_type: CoinbaseOrderType | None = None


class CreateOrderSuccess(_FrozenBase):
    order_id: str
    product_id: str
    side: CoinbaseOrderSide
    client_order_id: str


class CreateOrderResponse(_FrozenBase):
    success: bool
    order_id: str | None = None
    failure_reason: str | None = None
    success_response: CreateOrderSuccess | None = None


class CancelOrderResult(_FrozenBase):
    success: bool
    failure_reason: str | None = None
    order_id: str


class CancelOrdersResponse(_FrozenBase):
    results: list[CancelOrderResult]


class ListOrdersResponse(_FrozenBase):
    orders: list[Order]
    sequence: str | None = None
    has_next: bool = False
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------


class Fill(_FrozenBase):
    entry_id: str
    trade_id: str
    order_id: str
    trade_time: datetime
    trade_type: str
    price: str
    size: str
    commission: str
    product_id: str
    liquidity_indicator: str | None = None
    side: CoinbaseOrderSide


class ListFillsResponse(_FrozenBase):
    fills: list[Fill]
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class Product(_FrozenBase):
    product_id: str
    base_currency_id: str
    quote_currency_id: str
    base_increment: str
    quote_increment: str
    base_min_size: str
    base_max_size: str
    quote_min_size: str
    quote_max_size: str
    status: str | None = None
    trading_disabled: bool = False
    is_disabled: bool = False
    new: bool = False
    cancel_only: bool = False
    limit_only: bool = False
    post_only: bool = False
    auction_mode: bool = False
    product_type: str | None = None
    price: str | None = None
    volume_24h: str | None = Field(default=None, alias="volume_24h")


class ListProductsResponse(_FrozenBase):
    products: list[Product]
    num_products: int = 0


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------


class Candle(_FrozenBase):
    start: str  # Unix timestamp string
    low: str
    high: str
    open: str
    close: str
    volume: str


class GetProductCandlesResponse(_FrozenBase):
    candles: list[Candle]


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------


class OrderBookLevel(_FrozenBase):
    price: str
    size: str


class PriceBook(_FrozenBase):
    product_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    time: datetime | None = None


class OrderBookResponse(_FrozenBase):
    pricebook: PriceBook


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class ServerTimeResponse(_FrozenBase):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    iso: str
    epoch_seconds: str = Field(alias="epochSeconds")
    epoch_millis: str = Field(alias="epochMillis")
