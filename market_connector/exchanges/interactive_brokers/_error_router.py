"""IB error code router — maps codes to domain exceptions, dispatches to waiters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    ConnectionTerminatedError,
    ContractNotFoundError,
    HistoricalDataError,
    IbError,
    MarketDataPermissionError,
    OrderCancelledError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)
from market_connector.hb_compat.logging import get_logger

if TYPE_CHECKING:
    from asyncio import Future
    from collections.abc import Callable

    from ib_async import Contract

logger = get_logger(__name__)


# Simple code -> exception mapping. Request-scoped codes fail the request waiter,
# OrderRejectedError codes the order waiter. Codes needing more than a lookup are
# handled in ``on_error``: 162 (text-disambiguated), 420/100 (rate limits),
# 10197 (competing session), 202 (cancellation), connectivity and farm codes.
_ERROR_TABLE: dict[int, type[IbError]] = {
    165: HistoricalDataError,
    200: ContractNotFoundError,
    201: OrderRejectedError,
    309: SubscriptionLimitError,
    321: OrderRejectedError,
    325: OrderRejectedError,
    354: MarketDataPermissionError,
    10089: MarketDataPermissionError,
}

# Order-scoped informational codes: IB is telling us about a preset, not failing the
# order. ib_async still flips the Trade to "Cancelled" locally for them, so they must
# never fail an order waiter or be treated as a cancellation. 10349 = TIF set to DAY.
INFORMATIONAL_ORDER_CODES = frozenset({10349})

# Connectivity lost: 1100 (lost), 2110 (TWS-to-server broken), 502/504 (API socket).
_CONNECTION_LOST_CODES = frozenset({1100, 2110, 502, 504})

# Data-farm status codes -> (farm, ok).
_FARM_CODES: dict[int, tuple[str, bool]] = {
    2103: ("market", False),
    2104: ("market", True),
    2105: ("hmds", False),
    2106: ("hmds", True),
    2157: ("secdef", False),
    2158: ("secdef", True),
}

# Suggested wait (seconds) before retrying after a rate-limit error.
# Historical-data pacing violations clear slowly (IB's pacing windows are ~10s);
# message-rate codes (100, 420) clear within about a second.
HISTORICAL_PACING_RETRY_AFTER = 10.0
MESSAGE_RATE_RETRY_AFTER = 1.0


_DELAYED_DATA_HINT = (
    "(delayed/frozen market data is configured; tick-by-tick and market-depth "
    "requests require live market data permissions)"
)


class _ErrorRouter:
    """Routes IB error events to in-flight call Futures and manages connection state."""

    def __init__(
        self,
        market_data_type: int | None = None,
        *,
        on_pacing_violation: Callable[[float | None], None] | None = None,
    ) -> None:
        self.market_data_type = market_data_type
        # Called as cb(retry_after) on a 162 pacing violation or a 420 message-rate error.
        self._on_pacing_violation = on_pacing_violation
        self._pending_request_waiters: dict[int, Future[Any]] = {}
        self._pending_order_waiters: dict[int, Future[Any]] = {}
        # Starts False: the transport flips it after a successful connect.
        self._is_connected: bool = False
        self._connection_listeners: list[Callable[[bool], None]] = []
        # Called as cb(code, msg) on 1101 (connectivity restored, data lost).
        self._data_lost_listeners: list[Callable[[int, str], None]] = []
        # Farms currently reported broken; farm listeners get cb(farm, ok, code, msg).
        self._stale_farms: set[str] = set()
        self._farm_listeners: list[Callable[[str, bool, int, str], None]] = []
        # order_id -> IB's code-202 cancellation text (always recorded).
        self._cancel_reasons: dict[int, str] = {}
        # Called as cb() on 317 (market depth RESET: listeners must empty their books).
        self._depth_reset_listeners: list[Callable[[], None]] = []
        # Called as cb(req_id, code, msg) for every error; stream owners filter by reqId.
        self._stream_error_listeners: list[Callable[[int, int, str], None]] = []

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def set_connected(self, is_up: bool) -> None:
        """Set the connection flag without notifying listeners.

        The transport owns the connect/disconnect lifecycle and publishes the
        resulting state itself; listeners are only for IB-originated codes.
        """
        self._is_connected = is_up

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

        # Code 1101: connectivity restored but market data was lost — flip state,
        # notify, then let data-lost listeners resubscribe their streams.
        if code == 1101:
            self._is_connected = True
            self._notify_connection(True)
            for data_cb in list(self._data_lost_listeners):
                data_cb(code, msg)
            return

        # Persistent-stream owners match req_id against their live requests (-1 never matches).
        for stream_cb in list(self._stream_error_listeners):
            stream_cb(req_id, code, msg)

        self._report_pacing(code, msg)

        # Code 10090: market data partially unavailable — log warning, do not raise.
        if code == 10090:
            logger.warning("IB error %d (partial data): %s", code, msg)
            return

        # Code 317: market depth was RESET — informational; books must be emptied.
        # Not request-scoped (never fails a waiter) and says nothing about connectivity.
        if code == 317:
            logger.info("IB error %d (market depth reset): %s", code, msg)
            for reset_cb in list(self._depth_reset_listeners):
                reset_cb()
            return

        if code in INFORMATIONAL_ORDER_CODES:
            logger.info("IB order info %d: %s", code, msg)
            return

        if code in _CONNECTION_LOST_CODES:
            self._is_connected = False
            self._notify_connection(False)
            self._fail_all_pending(ConnectionLostError(code, msg))
            return

        if code == 1300:
            self._fail_all_pending(ConnectionTerminatedError(code, msg))
            return

        farm = _FARM_CODES.get(code)
        if farm is not None:
            self._on_farm_status(farm[0], farm[1], code, msg)
            return

        if code == 202:
            self._cancel_reasons[req_id] = msg
            self._fail_waiter(
                self._pending_order_waiters, req_id, OrderCancelledError(code, msg, msg)
            )
            return

        exc_cls = _ERROR_TABLE.get(code)
        if exc_cls is OrderRejectedError:
            # IB protocol invariant: req_id == orderId for order-related errors.
            self._fail_waiter(self._pending_order_waiters, req_id, exc_cls(code, msg))
            return

        request_exc = self._request_exception(code, msg, exc_cls)
        if request_exc is None:
            # Unrouted error — log at debug.
            logger.debug("Unrouted IB error %d: %s", code, msg)
            return

        waiting = req_id in self._pending_request_waiters
        self._fail_waiter(self._pending_request_waiters, req_id, request_exc)
        if code == 309 and not waiting:
            logger.warning("IB error %d (subscription limit, no request waiting): %s", code, msg)

    def _report_pacing(self, code: int, msg: str) -> None:
        """Tell the pacer about a historical pacing violation (162) or message-rate error (420)."""
        if self._on_pacing_violation is None:
            return
        if code == 162 and "pacing violation" in msg.lower():
            self._on_pacing_violation(HISTORICAL_PACING_RETRY_AFTER)
        elif code == 420:
            self._on_pacing_violation(MESSAGE_RATE_RETRY_AFTER)

    def _request_exception(
        self, code: int, msg: str, exc_cls: type[IbError] | None
    ) -> IbError | None:
        """Build the exception for a request-scoped error code, or None if unrouted."""
        if code == 162:
            lowered = msg.lower()
            if "pacing violation" in lowered:
                return RateLimitError(code, msg, retry_after=HISTORICAL_PACING_RETRY_AFTER)
            exc_cls = (
                MarketDataPermissionError
                if "no market data permissions" in lowered
                else HistoricalDataError
            )
        elif code in (420, 100):
            return RateLimitError(code, msg, retry_after=MESSAGE_RATE_RETRY_AFTER)
        elif code == 10197:
            return MarketDataPermissionError(
                code, f"{msg} (competing live session is using the market data)"
            )
        if exc_cls is None:
            return None
        if exc_cls is MarketDataPermissionError and self.market_data_type not in (None, 1):
            msg = f"{msg} {_DELAYED_DATA_HINT}"
        return exc_cls(code, msg)

    @staticmethod
    def _fail_waiter(waiters: dict[int, Future[Any]], key: int, exc: IbError) -> None:
        fut = waiters.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_exception(exc)

    def _on_farm_status(self, farm: str, ok: bool, code: int, msg: str) -> None:
        if ok:
            self._stale_farms.discard(farm)
        else:
            self._stale_farms.add(farm)
        for farm_cb in list(self._farm_listeners):
            farm_cb(farm, ok, code, msg)

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
