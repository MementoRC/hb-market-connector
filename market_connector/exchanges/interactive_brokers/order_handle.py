"""IB order state model and handle wrapper for Stage 2."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.interactive_brokers._error_router import (
    INFORMATIONAL_ORDER_CODES,
)

if TYPE_CHECKING:
    from ib_async import Trade


class IBOrderState(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


_TRADE_STATUS_MAP: dict[str, IBOrderState] = {
    "PendingSubmit": IBOrderState.PENDING,
    # IB has accepted the order at PreSubmitted (permId assigned); only
    # PendingSubmit/ApiPending are unacknowledged.
    "PreSubmitted": IBOrderState.SUBMITTED,
    "PendingCancel": IBOrderState.PENDING,
    "Submitted": IBOrderState.SUBMITTED,
    # ApiPending is unacknowledged (IB has not accepted the order yet), like PendingSubmit.
    "ApiPending": IBOrderState.PENDING,
    "Filled": IBOrderState.FILLED,
    "Cancelled": IBOrderState.CANCELLED,
    "ApiCancelled": IBOrderState.CANCELLED,
    "Inactive": IBOrderState.REJECTED,
}

_UNACKNOWLEDGED_STATUSES = frozenset({"PendingSubmit", "ApiPending"})


_VALIDATION_ERROR = "ValidationError"


def _effective_status(trade: Trade) -> str:
    """IB status of *trade*, looking through ib_async's ``ValidationError`` overlay.

    ib_async overwrites the status with "ValidationError" on warning codes (e.g. 399)
    and never restores it, though it is a warning and not an IB order state. Recover
    the latest real status from the trade log; failing that, infer it from permId.
    A permId means IB accepted the order, so an unacknowledged log status is stale.
    """
    raw: str = trade.orderStatus.status
    if raw != _VALIDATION_ERROR:
        return raw
    prior = "PendingSubmit"
    for entry in reversed(trade.log):
        if entry.status != _VALIDATION_ERROR and entry.status in _TRADE_STATUS_MAP:
            prior = str(entry.status)
            break
    if prior in _UNACKNOWLEDGED_STATUSES and trade.order.permId:
        return "Submitted"
    return prior


def is_unacknowledged(trade: Trade) -> bool:
    """True while IB has not yet acknowledged the order (PendingSubmit/ApiPending)."""
    return _effective_status(trade) in _UNACKNOWLEDGED_STATUSES


def is_synthetic_cancel(trade: Trade) -> bool:
    """True when ib_async cancelled a live order locally on an informational code.

    IB has not cancelled anything (no permId yet, and IB goes on to work the order),
    so such a trade must not be reported as CANCELLED.
    """
    if trade.orderStatus.status != "Cancelled" or trade.order.permId:
        return False
    return any(entry.errorCode in INFORMATIONAL_ORDER_CODES for entry in trade.log)


@dataclass(frozen=True)
class OrderHandle:
    order_id: int
    status: IBOrderState
    raw_status: str
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    _trade: Trade
    client_id: int = 0
    owned: bool = True
    manual: bool = False

    @classmethod
    def from_trade(cls, trade: Trade, own_client_id: int | None = None) -> OrderHandle:
        """Snapshot *trade*; ``owned`` is True when *own_client_id* is None or matches."""
        raw = trade.orderStatus.status
        effective = _effective_status(trade)
        if is_synthetic_cancel(trade):
            base_state = IBOrderState.PENDING
        else:
            try:
                base_state = _TRADE_STATUS_MAP[effective]
            except KeyError as exc:
                raise ValueError(f"Unknown IB order status: {raw}") from exc
        raw_filled = trade.orderStatus.filled  # None when unset (ib_async 3.0 default)
        filled = Decimal(0) if raw_filled is None else Decimal(str(raw_filled))
        # promote to PARTIALLY_FILLED if fills present but base state is SUBMITTED
        if base_state is IBOrderState.SUBMITTED and filled > 0:
            base_state = IBOrderState.PARTIALLY_FILLED
        avg_px = trade.orderStatus.avgFillPrice
        client_id = trade.order.clientId
        return cls(
            order_id=trade.order.permId if trade.order.permId else trade.order.orderId,
            status=base_state,
            raw_status=raw,
            filled_qty=filled,
            avg_fill_price=Decimal(str(avg_px)) if avg_px else None,
            _trade=trade,
            client_id=client_id,
            owned=own_client_id is None or client_id == own_client_id,
            manual=client_id == 0 and trade.order.orderId <= 0,
        )

    async def wait_for(
        self,
        *,
        status: IBOrderState | set[IBOrderState],
        timeout: float,
    ) -> OrderHandle:
        """Await until the trade reaches one of the target states.

        Raises asyncio.TimeoutError on timeout. Raises RuntimeError if
        a terminal state is reached that is not in the awaited set.
        """
        targets = {status} if isinstance(status, IBOrderState) else set(status)
        terminals = {IBOrderState.FILLED, IBOrderState.CANCELLED, IBOrderState.REJECTED}

        fut: asyncio.Future[OrderHandle] = asyncio.get_running_loop().create_future()

        def _on_status(*_: Any) -> None:
            try:
                new_handle = replace(OrderHandle.from_trade(self._trade), owned=self.owned)
            except ValueError:
                return
            if new_handle.status in targets:
                if not fut.done():
                    fut.set_result(new_handle)
            elif new_handle.status in terminals and not fut.done():
                fut.set_exception(
                    RuntimeError(
                        f"Awaited {targets} but order reached terminal state {new_handle.status}"
                    )
                )

        self._trade.statusEvent += _on_status
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._trade.statusEvent -= _on_status
