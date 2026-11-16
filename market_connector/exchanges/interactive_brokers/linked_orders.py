"""Linked IB orders: bracket (parent + take-profit + stop-loss) and OCA groups."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from typing import TYPE_CHECKING

from data_type_primitives import CancelOutcome, LinkedCancelOutcome, LinkedCancelStatus

from market_connector.exchanges.interactive_brokers.exceptions import IbError
from market_connector.exchanges.interactive_brokers.order_handle import IBOrderState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle

_NOT_FOUND_CODE = 10147
_TERMINAL = frozenset(
    {CancelOutcome.CANCELED, CancelOutcome.ALREADY_FILLED, CancelOutcome.NOT_FOUND}
)


async def _cancel_leg(
    cancel: Callable[[OrderHandle], Awaitable[OrderHandle]], leg: OrderHandle
) -> tuple[CancelOutcome, Decimal, bool]:
    """Cancel one leg: (outcome, filled_qty, errored). Never raises for a per-leg failure."""
    try:
        after = await cancel(leg)
    except IbError as exc:
        outcome = (
            CancelOutcome.NOT_FOUND if exc.error_code == _NOT_FOUND_CODE else CancelOutcome.UNKNOWN
        )
        return outcome, leg.filled_qty, outcome is CancelOutcome.UNKNOWN
    except Exception:
        return CancelOutcome.UNKNOWN, leg.filled_qty, True
    if after.status == IBOrderState.CANCELLED:
        return CancelOutcome.CANCELED, after.filled_qty, False
    if after.status == IBOrderState.FILLED:
        return CancelOutcome.ALREADY_FILLED, after.filled_qty, False
    return CancelOutcome.UNKNOWN, after.filled_qty, False


def _linked_status(outcomes: list[CancelOutcome], errored: bool) -> LinkedCancelStatus:
    if CancelOutcome.ALREADY_FILLED in outcomes:
        return LinkedCancelStatus.FILLED
    if all(o in _TERMINAL for o in outcomes):
        return LinkedCancelStatus.CANCELED
    return LinkedCancelStatus.UNKNOWN if errored else LinkedCancelStatus.PENDING


async def cancel_legs(
    cancel: Callable[[OrderHandle], Awaitable[OrderHandle]],
    group_id: str,
    legs: list[OrderHandle],
) -> LinkedCancelOutcome:
    """Cancel every placed leg once and summarise as a dtp ``LinkedCancelOutcome``.

    ``residual_filled`` is signed net exposure relative to ``legs[0]``'s side.
    """
    outcomes: list[CancelOutcome] = []
    residual = Decimal(0)
    errored = False
    first_action = legs[0].action
    for leg in legs:
        outcome, filled, leg_errored = await _cancel_leg(cancel, leg)
        outcomes.append(outcome)
        errored = errored or leg_errored
        residual += filled if leg.action == first_action else -filled
    return LinkedCancelOutcome(
        group_id=group_id,
        all_legs_terminal=all(o in _TERMINAL for o in outcomes),
        residual_filled=residual,
        leg_outcomes=tuple(outcomes),
        status=_linked_status(outcomes, errored),
    )


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
