"""Coinbase order status -> ``OrderState`` mapping (shared by leg lookup, #250)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from data_type_primitives import OrderState

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = ["COINBASE_STATUS_TO_ORDER_STATE", "to_order_state"]

# Keys are the declared ``CoinbaseOrderStatus`` wire values. EXPIRED is terminal without a
# fill, so it is CANCELED. UNKNOWN_ORDER_STATUS maps to ``OrderState.UNKNOWN`` (never to a
# terminal state) so a caller cannot mistake an unrecognised status for a finished order.
COINBASE_STATUS_TO_ORDER_STATE: dict[str, OrderState] = {
    "PENDING": OrderState.PENDING_CREATE,
    "QUEUED": OrderState.PENDING_CREATE,
    "CANCEL_QUEUED": OrderState.PENDING_CANCEL,
    "EDIT_QUEUED": OrderState.OPEN,
    "OPEN": OrderState.OPEN,
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELED,
    "EXPIRED": OrderState.CANCELED,
    "FAILED": OrderState.FAILED,
    "UNKNOWN_ORDER_STATUS": OrderState.UNKNOWN,
}


def to_order_state(status: str, filled: Decimal) -> OrderState:
    """Map a Coinbase status; an OPEN order with ``filled > 0`` is PARTIALLY_FILLED."""
    state = COINBASE_STATUS_TO_ORDER_STATE.get(status, OrderState.UNKNOWN)
    if state is OrderState.OPEN and filled > 0:
        return OrderState.PARTIALLY_FILLED
    return state
