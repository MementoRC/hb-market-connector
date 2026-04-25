"""Tests for market_connector.exchanges.coinbase.converters — pure conversion functions."""

from decimal import Decimal

from market_connector.exchanges.coinbase.converters import (
    from_exchange_pair,
    to_balance,
    to_candle,
    to_exchange_pair,
    to_open_order,
    to_orderbook_snapshot,
    to_orderbook_update,
    to_trade_event,
)
from market_connector.exchanges.coinbase.schemas.rest import (
    Account,
    Balance,
    Candle,
    LimitGTCConfig,
    Order,
    OrderBookLevel,
    OrderBookResponse,
    OrderConfiguration,
    PriceBook,
)
from market_connector.exchanges.coinbase.schemas.ws import Level2Event, Level2Update, MarketTrade
from market_connector.primitives import OrderType, TradeType


def test_to_exchange_pair_passthrough():
    assert to_exchange_pair("BTC-USD") == "BTC-USD"


def test_from_exchange_pair_passthrough():
    assert from_exchange_pair("BTC-USD") == "BTC-USD"


# ---------------------------------------------------------------------------
# 4.2  Balance converter
# ---------------------------------------------------------------------------


def test_to_balance_extracts_available():
    account = Account(
        uuid="u1",
        name="BTC Wallet",
        currency="BTC",
        available_balance=Balance(value="1.5", currency="BTC"),
        hold=Balance(value="0.3", currency="BTC"),
    )
    assert to_balance(account) == Decimal("1.5")


# ---------------------------------------------------------------------------
# 4.3  Order converter
# ---------------------------------------------------------------------------


def test_to_open_order_from_limit():
    order = Order(
        order_id="o1",
        client_order_id="c1",
        product_id="BTC-USD",
        side="BUY",
        status="OPEN",
        order_configuration=OrderConfiguration(
            limit_limit_gtc=LimitGTCConfig(base_size="0.5", limit_price="50000"),
        ),
        filled_size="0.1",
        average_filled_price="50000",
    )
    result = to_open_order(order)
    assert result.exchange_order_id == "o1"
    assert result.client_order_id == "c1"
    assert result.trading_pair == "BTC-USD"
    assert result.side == TradeType.BUY
    assert result.amount == Decimal("0.5")
    assert result.price == Decimal("50000")
    assert result.filled_amount == Decimal("0.1")
    assert result.order_type == OrderType.LIMIT


def test_to_open_order_from_limit_maker():
    from market_connector.exchanges.coinbase.schemas.rest import LimitGTCConfig, OrderConfiguration

    order = Order(
        order_id="o2",
        client_order_id="c2",
        product_id="BTC-USD",
        side="SELL",
        status="OPEN",
        order_configuration=OrderConfiguration(
            limit_limit_gtc=LimitGTCConfig(base_size="0.3", limit_price="60000", post_only=True),
        ),
        filled_size="0",
        average_filled_price="0",
    )
    result = to_open_order(order)
    assert result.order_type == OrderType.LIMIT_MAKER


def test_to_open_order_from_limit_gtd():
    from market_connector.exchanges.coinbase.schemas.rest import LimitGTDConfig, OrderConfiguration

    order = Order(
        order_id="o3",
        client_order_id="c3",
        product_id="BTC-USD",
        side="BUY",
        status="OPEN",
        order_configuration=OrderConfiguration(
            limit_limit_gtd=LimitGTDConfig(
                base_size="0.2", limit_price="55000", end_time="2026-05-01T00:00:00Z"
            ),
        ),
        filled_size="0",
        average_filled_price="0",
    )
    result = to_open_order(order)
    assert result.order_type == OrderType.LIMIT
    assert result.amount == Decimal("0.2")


def test_to_open_order_from_market_ioc():
    from market_connector.exchanges.coinbase.schemas.rest import MarketIOCConfig, OrderConfiguration

    order = Order(
        order_id="o4",
        client_order_id="c4",
        product_id="BTC-USD",
        side="BUY",
        status="FILLED",
        order_configuration=OrderConfiguration(
            market_market_ioc=MarketIOCConfig(base_size="0.1"),
        ),
        filled_size="0.1",
        average_filled_price="50000",
    )
    result = to_open_order(order)
    assert result.order_type == OrderType.MARKET
    assert result.amount == Decimal("0.1")
    assert result.price == Decimal("0")


def test_to_open_order_no_configuration_fallback():
    order = Order(
        order_id="o5",
        client_order_id="c5",
        product_id="BTC-USD",
        side="BUY",
        status="OPEN",
        order_configuration=None,
        filled_size="0",
        average_filled_price="0",
    )
    result = to_open_order(order)
    assert result.order_type == OrderType.LIMIT
    assert result.amount == Decimal("0")
    assert result.price == Decimal("0")


# ---------------------------------------------------------------------------
# 4.4  Orderbook converters
# ---------------------------------------------------------------------------


def test_to_orderbook_snapshot():
    book = OrderBookResponse(
        pricebook=PriceBook(
            product_id="BTC-USD",
            bids=[OrderBookLevel(price="50000", size="0.5")],
            asks=[OrderBookLevel(price="50001", size="0.3")],
        )
    )
    snap = to_orderbook_snapshot(book)
    assert snap.trading_pair == "BTC-USD"
    assert snap.bids == [(Decimal("50000"), Decimal("0.5"))]
    assert snap.asks == [(Decimal("50001"), Decimal("0.3"))]


def test_to_orderbook_update():
    evt = Level2Event(
        type="update",
        product_id="BTC-USD",
        updates=[
            Level2Update(side="bid", event_time="t", price_level="50000", new_quantity="0.5"),
            Level2Update(side="offer", event_time="t", price_level="50001", new_quantity="0.3"),
        ],
    )
    upd = to_orderbook_update(evt, update_id=42)
    assert upd.trading_pair == "BTC-USD"
    assert upd.bids == [(Decimal("50000"), Decimal("0.5"))]
    assert upd.asks == [(Decimal("50001"), Decimal("0.3"))]
    assert upd.update_id == 42


# ---------------------------------------------------------------------------
# 4.5  Trade and candle converters
# ---------------------------------------------------------------------------


def test_to_trade_event():
    trade = MarketTrade(
        trade_id="t1",
        product_id="BTC-USD",
        price="50000",
        size="0.5",
        side="BUY",
        time="2026-04-24T12:00:00Z",
    )
    evt = to_trade_event(trade)
    assert evt.exchange_trade_id == "t1"
    assert evt.trading_pair == "BTC-USD"
    assert evt.price == Decimal("50000")
    assert evt.amount == Decimal("0.5")
    assert evt.side == TradeType.BUY


def test_to_candle_format():
    candle = Candle(
        start="1714000000",
        low="49000",
        high="51000",
        open="50000",
        close="50500",
        volume="10.5",
    )
    result = to_candle(candle)
    assert result == [
        1714000000,
        Decimal("50000"),
        Decimal("51000"),
        Decimal("49000"),
        Decimal("50500"),
        Decimal("10.5"),
    ]
