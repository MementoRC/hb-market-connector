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

import warnings
from decimal import Decimal  # noqa: TCH003
from enum import StrEnum
from pathlib import Path
from typing import Any

import pydantic
from data_type_primitives import TriggerKind, TriggerSpec
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from market_connector.hb_compat.primitives import coerce_order_type, coerce_trade_type, to_canonical
from market_connector.primitives import OrderType, TradeType

__all__ = ["HBOrder", "OrderType", "TimeInForce", "TradeType"]

_CONDITIONAL_TYPES = frozenset({OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT})
# Skip pydantic/this-module frames so the DeprecationWarning points at the HBOrder(...) caller.
_WARN_SKIP_PREFIXES = (str(Path(pydantic.__file__).parent), __file__)


class TimeInForce(StrEnum):
    """Order lifetime: DAY (session), GTC (good till cancelled), IOC (immediate or cancel)."""

    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    OPG = "OPG"


class HBOrder(BaseModel):
    """Immutable order placement request.

    Fields mirror the ExecutionGateway.place_order signature so that the
    gateway can pass a structured object to typed transport methods rather
    than a flat parameter list.
    """

    model_config = ConfigDict(frozen=True)

    order_type: OrderType
    side: TradeType

    @field_validator("order_type", mode="before")
    @classmethod
    def _coerce_order_type(cls, value: object) -> OrderType:
        return coerce_order_type(value)

    @field_validator("side", mode="before")
    @classmethod
    def _coerce_side(cls, value: object) -> TradeType:
        return coerce_trade_type(value)

    amount: Decimal
    price: Decimal | None = None
    # Deprecated: use ``trigger``. Mapped to an ABSOLUTE ``TriggerSpec`` on construction.
    stop_price: Decimal | None = None
    trigger: TriggerSpec | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    outside_rth: bool = False

    @model_validator(mode="before")
    @classmethod
    def _map_stop_price(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("stop_price") is None:
            return data
        if data.get("trigger") is not None:
            raise ValueError("specify either stop_price (deprecated) or trigger, not both")
        warnings.warn(
            "HBOrder.stop_price is deprecated; use trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, "
            "value=...) instead",
            DeprecationWarning,
            skip_file_prefixes=_WARN_SKIP_PREFIXES,
        )
        return {
            **data,
            "trigger": TriggerSpec(kind=TriggerKind.ABSOLUTE, value=data["stop_price"]),
        }

    @model_validator(mode="after")
    def _check_trigger(self) -> HBOrder:
        conditional = self.order_type in _CONDITIONAL_TYPES
        if self.trigger is None:
            if conditional:
                raise ValueError(f"{self.order_type.name} requires a trigger")
            return self
        if not conditional:
            raise ValueError(
                f"trigger is only valid for conditional order types, not {self.order_type.name}"
            )
        self.trigger.validate_for(to_canonical(self.order_type))  # type: ignore[arg-type]
        if self.order_type == OrderType.STOP_LOSS_LIMIT and self.price is None:
            raise ValueError("STOP_LOSS_LIMIT order requires a price")
        return self
