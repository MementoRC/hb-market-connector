"""IB error code router — maps codes to domain exceptions, dispatches to waiters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    ConnectionTerminatedError,
    ContractNotFoundError,
    IbError,
    OrderRejectedError,
)

if TYPE_CHECKING:
    from asyncio import Future
    from collections.abc import Callable

    from ib_async import Contract  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


_ERROR_TABLE: dict[int, type[IbError]] = {
    162: ContractNotFoundError,
    200: ContractNotFoundError,
    201: OrderRejectedError,
    321: OrderRejectedError,
    325: OrderRejectedError,
    1100: ConnectionLostError,
    1300: ConnectionTerminatedError,
}


class _ErrorRouter:
    """Routes IB error events to in-flight call Futures and manages connection state."""

    def __init__(self) -> None:
        self._pending_request_waiters: dict[int, Future[Any]] = {}
        self._pending_order_waiters: dict[int, Future[Any]] = {}
        self._is_connected: bool = True
        self._connection_listeners: list[Callable[[bool], None]] = []

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def on_error(
        self,
        req_id: int,
        code: int,
        msg: str,
        contract: Contract | None = None,
    ) -> None:
        """Handle a single IB errorEvent callback."""
        # Code 1102: connectivity restored — flip state, notify, return.
        if code == 1102:
            self._is_connected = True
            self._notify_connection(True)
            return

        exc_cls = _ERROR_TABLE.get(code)

        if exc_cls is ConnectionLostError:
            self._is_connected = False
            self._notify_connection(False)
            self._fail_all_pending(exc_cls(code, msg))
            return

        if exc_cls is ConnectionTerminatedError:
            self._fail_all_pending(exc_cls(code, msg))
            return

        if exc_cls is ContractNotFoundError:
            fut = self._pending_request_waiters.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_exception(exc_cls(code, msg))
            return

        if exc_cls is OrderRejectedError:
            # IB protocol invariant: req_id == orderId for order-related errors.
            fut = self._pending_order_waiters.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_exception(exc_cls(code, msg))
            return

        # Unrouted error — log at debug; Stage 6 may extend.
        logger.debug("Unrouted IB error %d: %s", code, msg)

    def _fail_all_pending(self, exc: IbError) -> None:
        all_futs = list(self._pending_request_waiters.values()) + list(
            self._pending_order_waiters.values()
        )
        for fut in all_futs:
            if not fut.done():
                fut.set_exception(exc)
        self._pending_request_waiters.clear()
        self._pending_order_waiters.clear()

    def _notify_connection(self, is_up: bool) -> None:
        for cb in self._connection_listeners:
            cb(is_up)
