"""Order request primitive for the exchange gateway framework.

HBOrder is the input type passed to ExecutionGateway.place_order and the
transport-level _hb_to_ib_order mapper. It carries the fields that
describe a placement request (including time-in-force, outside-RTH and stop price);
exchange-specific connectors consume it and produce connector-native
order objects.

OrderType and TradeType are re-exported here so callers can do:

    from market_connector.orders import HBOrder, OrderType, TradeType
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TCH003
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from market_connector.primitives import OrderType, TradeType

__all__ = ["HBOrder", "OrderType", "TimeInForce", "TradeType"]


class TimeInForce(StrEnum):
    """Order lifetime: DAY (session), GTC (good till cancelled), IOC (immediate or cancel)."""

    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"


class HBOrder(BaseModel):
    """Immutable order placement request.

    Fields mirror the ExecutionGateway.place_order signature so that the
    gateway can pass a structured object to typed transport methods rather
    than a flat parameter list.
    """

    model_config = ConfigDict(frozen=True)

    order_type: OrderType
    side: TradeType
    amount: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    outside_rth: bool = False
