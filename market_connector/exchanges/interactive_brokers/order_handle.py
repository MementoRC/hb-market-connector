"""IB order state model and handle wrapper for Stage 2."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_async import Trade


class OrderState(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


_TRADE_STATUS_MAP: dict[str, OrderState] = {
    "PendingSubmit": OrderState.PENDING,
    "PreSubmitted": OrderState.PENDING,
    "PendingCancel": OrderState.PENDING,
    "Submitted": OrderState.SUBMITTED,
    "ApiPending": OrderState.SUBMITTED,
    "Filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELLED,
    "ApiCancelled": OrderState.CANCELLED,
    "Inactive": OrderState.REJECTED,
}


@dataclass(frozen=True)
class OrderHandle:
    order_id: int
    status: OrderState
    raw_status: str
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    _trade: "Trade"

    @classmethod
    def from_trade(cls, trade: "Trade") -> "OrderHandle":
        raw = trade.orderStatus.status
        try:
            base_state = _TRADE_STATUS_MAP[raw]
        except KeyError as exc:
            raise ValueError(f"Unknown IB order status: {raw}") from exc
        filled = Decimal(str(trade.orderStatus.filled))
        # promote to PARTIALLY_FILLED if fills present but base state is SUBMITTED
        if base_state is OrderState.SUBMITTED and filled > 0:
            base_state = OrderState.PARTIALLY_FILLED
        avg_px = trade.orderStatus.avgFillPrice
        return cls(
            order_id=trade.order.permId if trade.order.permId else trade.order.orderId,
            status=base_state,
            raw_status=raw,
            filled_qty=filled,
            avg_fill_price=Decimal(str(avg_px)) if avg_px else None,
            _trade=trade,
        )
