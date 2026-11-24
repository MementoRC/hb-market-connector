"""IB ``orderStatus.status`` -> ``OrderState`` mapping for leg lookup (#250)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from data_type_primitives import OrderState

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = ["IB_STATUS_TO_ORDER_STATE", "to_order_state"]

# Unrecognised statuses map to UNKNOWN (never a terminal state). Inactive means the order was
# rejected or is dormant at IB, so it is FAILED.
IB_STATUS_TO_ORDER_STATE: dict[str, OrderState] = {
    "PendingSubmit": OrderState.PENDING_CREATE,
    "ApiPending": OrderState.PENDING_CREATE,
    "PreSubmitted": OrderState.OPEN,
    "Submitted": OrderState.OPEN,
    "PendingCancel": OrderState.PENDING_CANCEL,
    "Filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELED,
    "ApiCancelled": OrderState.CANCELED,
    "Inactive": OrderState.FAILED,
}


def to_order_state(status: str, filled: Decimal) -> OrderState:
    """Map an IB status; an OPEN order with ``filled > 0`` is PARTIALLY_FILLED."""
    state = IB_STATUS_TO_ORDER_STATE.get(status, OrderState.UNKNOWN)
    if state is OrderState.OPEN and filled > 0:
        return OrderState.PARTIALLY_FILLED
    return state
