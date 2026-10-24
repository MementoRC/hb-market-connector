"""Linked IB orders: bracket (parent + take-profit + stop-loss) and OCA groups."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle


class OcaType(IntEnum):
    """IB ``ocaType``: how the remaining members of an OCA group react to a fill."""

    CANCEL_WITH_BLOCK = 1
    REDUCE_WITH_BLOCK = 2
    REDUCE_NO_BLOCK = 3


@dataclass(frozen=True)
class IbBracket:
    """Handles of a placed bracket; the children are held by IB until the parent fills."""

    parent: OrderHandle
    take_profit: OrderHandle
    stop_loss: OrderHandle


@dataclass(frozen=True)
class IbOcaGroup:
    """Handles of a placed OCA group, in the order the orders were given."""

    group: str
    oca_type: OcaType
    orders: tuple[OrderHandle, ...]
