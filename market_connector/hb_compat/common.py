"""Re-export of the common data-type enums used by market_connector.

The enums come from hb-data-type-primitives. Hummingbot's own
``hummingbot.core.data_type.common`` re-exports the same classes, so they are
identical to the parent's ``OrderType`` / ``TradeType``. Sourcing them here keeps
market_connector free of a hummingbot import (#194, ADR 0001).
"""

from data_type_primitives.common import OrderType, TradeType

__all__ = ["OrderType", "TradeType"]
