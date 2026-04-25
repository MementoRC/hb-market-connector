"""Tests for Coinbase enum schema types."""

from market_connector.exchanges.coinbase.schemas.enums import (
    CoinbaseGranularity,
    CoinbaseOrderSide,
    CoinbaseOrderStatus,
    CoinbaseOrderType,
    CoinbaseProductType,
    CoinbaseTimeInForce,
    CoinbaseWsChannel,
    CoinbaseWsEventType,
)


def test_order_status_values():
    assert CoinbaseOrderStatus.OPEN == "OPEN"
    assert CoinbaseOrderStatus.FILLED == "FILLED"
    assert CoinbaseOrderStatus.CANCELLED == "CANCELLED"
    assert CoinbaseOrderStatus.EXPIRED == "EXPIRED"
    assert CoinbaseOrderStatus.FAILED == "FAILED"
    assert CoinbaseOrderStatus.PENDING == "PENDING"
    assert CoinbaseOrderStatus.UNKNOWN == "UNKNOWN_ORDER_STATUS"


def test_order_side_values():
    assert CoinbaseOrderSide.BUY == "BUY"
    assert CoinbaseOrderSide.SELL == "SELL"
    assert CoinbaseOrderSide.UNKNOWN == "UNKNOWN_ORDER_SIDE"


def test_order_type_values():
    assert CoinbaseOrderType.MARKET == "MARKET"
    assert CoinbaseOrderType.LIMIT == "LIMIT"
    assert CoinbaseOrderType.STOP == "STOP"
    assert CoinbaseOrderType.STOP_LIMIT == "STOP_LIMIT"
    assert CoinbaseOrderType.UNKNOWN == "UNKNOWN_ORDER_TYPE"


def test_time_in_force_values():
    assert CoinbaseTimeInForce.GTC == "GOOD_UNTIL_CANCELLED"
    assert CoinbaseTimeInForce.GTD == "GOOD_UNTIL_DATE_TIME"
    assert CoinbaseTimeInForce.IOC == "IMMEDIATE_OR_CANCEL"
    assert CoinbaseTimeInForce.FOK == "FILL_OR_KILL"


def test_product_type_values():
    assert CoinbaseProductType.SPOT == "SPOT"
    assert CoinbaseProductType.FUTURE == "FUTURE"


def test_granularity_values():
    assert CoinbaseGranularity.ONE_MINUTE == "ONE_MINUTE"
    assert CoinbaseGranularity.FIVE_MINUTE == "FIVE_MINUTE"
    assert CoinbaseGranularity.FIFTEEN_MINUTE == "FIFTEEN_MINUTE"
    assert CoinbaseGranularity.ONE_HOUR == "ONE_HOUR"
    assert CoinbaseGranularity.SIX_HOUR == "SIX_HOUR"
    assert CoinbaseGranularity.ONE_DAY == "ONE_DAY"


def test_ws_channels():
    assert CoinbaseWsChannel.LEVEL2 == "level2"
    assert CoinbaseWsChannel.MARKET_TRADES == "market_trades"
    assert CoinbaseWsChannel.USER == "user"
    assert CoinbaseWsChannel.CANDLES == "candles"
    assert CoinbaseWsChannel.TICKER == "ticker"
    assert CoinbaseWsChannel.STATUS == "status"


def test_ws_event_type_values():
    assert CoinbaseWsEventType.SNAPSHOT == "snapshot"
    assert CoinbaseWsEventType.UPDATE == "update"


def test_enums_are_str_comparable():
    """StrEnum members compare equal to plain strings."""
    assert CoinbaseOrderSide.BUY == "BUY"
    assert str(CoinbaseOrderSide.BUY) == "BUY"
