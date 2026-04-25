"""Coinbase Advanced Trade API enum types."""

from enum import StrEnum


class CoinbaseOrderStatus(StrEnum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN_ORDER_STATUS"


class CoinbaseOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN_ORDER_SIDE"


class CoinbaseOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    UNKNOWN = "UNKNOWN_ORDER_TYPE"


class CoinbaseTimeInForce(StrEnum):
    GTC = "GOOD_UNTIL_CANCELLED"
    GTD = "GOOD_UNTIL_DATE_TIME"
    IOC = "IMMEDIATE_OR_CANCEL"
    FOK = "FILL_OR_KILL"


class CoinbaseProductType(StrEnum):
    SPOT = "SPOT"
    FUTURE = "FUTURE"


class CoinbaseGranularity(StrEnum):
    ONE_MINUTE = "ONE_MINUTE"
    FIVE_MINUTE = "FIVE_MINUTE"
    FIFTEEN_MINUTE = "FIFTEEN_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    SIX_HOUR = "SIX_HOUR"
    ONE_DAY = "ONE_DAY"


class CoinbaseWsChannel(StrEnum):
    LEVEL2 = "level2"
    MARKET_TRADES = "market_trades"
    USER = "user"
    CANDLES = "candles"
    TICKER = "ticker"
    STATUS = "status"


class CoinbaseWsEventType(StrEnum):
    SNAPSHOT = "snapshot"
    UPDATE = "update"
