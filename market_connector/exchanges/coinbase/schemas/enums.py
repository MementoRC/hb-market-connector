"""Coinbase Advanced Trade API enum types."""

from enum import Enum


class _StrValue(str, Enum):
    """Base mixin: str(member) returns the value, matching StrEnum behaviour on Python 3.11+."""

    def __str__(self) -> str:
        return str.__str__(self)


class CoinbaseOrderStatus(_StrValue):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN_ORDER_STATUS"


class CoinbaseOrderSide(_StrValue):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN_ORDER_SIDE"


class CoinbaseOrderType(_StrValue):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    UNKNOWN = "UNKNOWN_ORDER_TYPE"


class CoinbaseTimeInForce(_StrValue):
    GTC = "GOOD_UNTIL_CANCELLED"
    GTD = "GOOD_UNTIL_DATE_TIME"
    IOC = "IMMEDIATE_OR_CANCEL"
    FOK = "FILL_OR_KILL"


class CoinbaseProductType(_StrValue):
    SPOT = "SPOT"
    FUTURE = "FUTURE"


class CoinbaseGranularity(_StrValue):
    ONE_MINUTE = "ONE_MINUTE"
    FIVE_MINUTE = "FIVE_MINUTE"
    FIFTEEN_MINUTE = "FIFTEEN_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    SIX_HOUR = "SIX_HOUR"
    ONE_DAY = "ONE_DAY"


class CoinbaseWsChannel(_StrValue):
    LEVEL2 = "level2"
    MARKET_TRADES = "market_trades"
    USER = "user"
    CANDLES = "candles"
    TICKER = "ticker"
    STATUS = "status"


class CoinbaseWsEventType(_StrValue):
    SNAPSHOT = "snapshot"
    UPDATE = "update"
