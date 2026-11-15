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
    """Handles of a placed bracket; the children are held by IB until the parent fills.

    Satisfies ``BracketHandle``: ``group_id`` is the parent's client order id. Either exit may
    be ``None`` for a single-exit bracket.
    """

    parent: OrderHandle
    take_profit: OrderHandle | None
    stop_loss: OrderHandle | None

    @property
    def group_id(self) -> int:
        return self.parent.client_order_id

    @property
    def entry_client_id(self) -> int:
        return self.parent.client_order_id

    @property
    def take_profit_client_id(self) -> int | None:
        return None if self.take_profit is None else self.take_profit.client_order_id

    @property
    def stop_loss_client_id(self) -> int | None:
        return None if self.stop_loss is None else self.stop_loss.client_order_id

    @property
    def emulated(self) -> bool:
        return False


@dataclass(frozen=True)
class IbOcaGroup:
    """Handles of a placed OCA group, in the order the orders were given.

    Satisfies ``OcoHandle``: ``group_id`` is the OCA group name.
    """

    group: str
    oca_type: OcaType
    orders: tuple[OrderHandle, ...]

    @property
    def group_id(self) -> str:
        return self.group

    @property
    def leg_client_ids(self) -> tuple[int, ...]:
        return tuple(o.client_order_id for o in self.orders)

    @property
    def emulated(self) -> bool:
        return False
