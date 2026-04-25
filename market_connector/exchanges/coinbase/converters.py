"""Pure conversion functions: Coinbase schemas → market-connector primitives."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from market_connector.exchanges.coinbase.schemas.rest import (
    Account,
    Candle,
    Order,
    OrderBookResponse,
    OrderConfiguration,
)
from market_connector.exchanges.coinbase.schemas.ws import Level2Event, MarketTrade
from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)

# ---------------------------------------------------------------------------
# 4.1  Pair passthrough
# ---------------------------------------------------------------------------


def to_exchange_pair(trading_pair: str) -> str:
    """Convert a Hummingbot trading pair to a Coinbase product_id (identity)."""
    return trading_pair


def from_exchange_pair(product_id: str) -> str:
    """Convert a Coinbase product_id to a Hummingbot trading pair (identity)."""
    return product_id


# ---------------------------------------------------------------------------
# 4.2  Balance converter
# ---------------------------------------------------------------------------


def to_balance(account: Account) -> Decimal:
    """Extract the available balance as Decimal from an Account schema."""
    return Decimal(account.available_balance.value)


# ---------------------------------------------------------------------------
# 4.3  Order converter
# ---------------------------------------------------------------------------

_SIDE_MAP: dict[str, TradeType] = {
    "BUY": TradeType.BUY,
    "SELL": TradeType.SELL,
}


def _extract_order_details(cfg: OrderConfiguration) -> tuple[OrderType, Decimal, Decimal]:
    """Return (order_type, amount, price) from an OrderConfiguration."""
    if cfg.limit_limit_gtc is not None:
        c = cfg.limit_limit_gtc
        ot = OrderType.LIMIT_MAKER if c.post_only else OrderType.LIMIT
        return ot, Decimal(c.base_size), Decimal(c.limit_price)
    if cfg.limit_limit_gtd is not None:
        c = cfg.limit_limit_gtd
        return OrderType.LIMIT, Decimal(c.base_size), Decimal(c.limit_price)
    if cfg.market_market_ioc is not None:
        mc = cfg.market_market_ioc
        size = mc.base_size or mc.quote_size or "0"
        return OrderType.MARKET, Decimal(size), Decimal("0")
    raise ValueError("Unsupported order configuration")


def to_open_order(order: Order) -> OpenOrder:
    """Convert a Coinbase REST Order schema to a market-connector OpenOrder primitive."""
    if order.order_configuration is not None:
        ot, amount, price = _extract_order_details(order.order_configuration)
    else:
        ot, amount, price = OrderType.LIMIT, Decimal("0"), Decimal("0")

    return OpenOrder(
        client_order_id=order.client_order_id,
        exchange_order_id=order.order_id,
        trading_pair=from_exchange_pair(order.product_id),
        order_type=ot,
        side=_SIDE_MAP[order.side.value],
        amount=amount,
        price=price,
        filled_amount=Decimal(order.filled_size),
        status=order.status.value,
    )


# ---------------------------------------------------------------------------
# 4.4  Orderbook converters
# ---------------------------------------------------------------------------


def to_orderbook_snapshot(book: OrderBookResponse) -> OrderBookSnapshot:
    """Convert a Coinbase REST OrderBookResponse to an OrderBookSnapshot primitive."""
    pb = book.pricebook
    return OrderBookSnapshot(
        trading_pair=from_exchange_pair(pb.product_id),
        bids=[(Decimal(level.price), Decimal(level.size)) for level in pb.bids],
        asks=[(Decimal(level.price), Decimal(level.size)) for level in pb.asks],
        timestamp=pb.time.timestamp() if pb.time else 0.0,
    )


def to_orderbook_update(event: Level2Event, update_id: int) -> OrderBookUpdate:
    """Convert a Coinbase WS Level2Event to an OrderBookUpdate primitive."""
    bids = [
        (Decimal(u.price_level), Decimal(u.new_quantity)) for u in event.updates if u.side == "bid"
    ]
    asks = [
        (Decimal(u.price_level), Decimal(u.new_quantity))
        for u in event.updates
        if u.side == "offer"
    ]
    return OrderBookUpdate(
        trading_pair=from_exchange_pair(event.product_id),
        bids=bids,
        asks=asks,
        update_id=update_id,
    )


# ---------------------------------------------------------------------------
# 4.5  Trade and candle converters
# ---------------------------------------------------------------------------


def to_trade_event(trade: MarketTrade) -> TradeEvent:
    """Convert a Coinbase WS MarketTrade to a TradeEvent primitive."""
    ts = datetime.fromisoformat(trade.time.replace("Z", "+00:00")).timestamp()
    return TradeEvent(
        exchange_trade_id=trade.trade_id,
        trading_pair=from_exchange_pair(trade.product_id),
        price=Decimal(trade.price),
        amount=Decimal(trade.size),
        side=_SIDE_MAP[trade.side],
        timestamp=ts,
    )


def to_candle(candle: Candle) -> list[Any]:
    """Convert a Coinbase REST Candle to OHLCV list: [timestamp, open, high, low, close, volume]."""
    return [
        int(candle.start),
        Decimal(candle.open),
        Decimal(candle.high),
        Decimal(candle.low),
        Decimal(candle.close),
        Decimal(candle.volume),
    ]
