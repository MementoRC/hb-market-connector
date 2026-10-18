"""Acceptance tests for extended IB error-code routing (issue #56, criteria C1..C9)."""

from __future__ import annotations

import asyncio
import logging
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    ConnectionTerminatedError,
    ContractNotFoundError,
    HistoricalDataError,
    MarketDataPermissionError,
    OrderCancelledError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    FARM_TOPIC,
    FarmStatusEvent,
    IbGatewayTransport,
    SubscriptionHandle,
)

from ._doubles import MockIb
from .test_transport_orders import _make_trade

if TYPE_CHECKING:
    from collections.abc import Iterator

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"

_FARM_BROKEN = [(2103, "market"), (2105, "hmds"), (2157, "secdef")]
_FARM_OK = [(2104, "market"), (2106, "hmds"), (2158, "secdef")]
_NEW_CODES = [
    2103, 2104, 2105, 2106, 2157, 2158, 2110, 502, 504, 10197, 162, 420, 100, 309, 202,
]  # fmt: skip


@pytest.fixture
def transport() -> Iterator[IbGatewayTransport]:
    """Connected-flagged transport over MockIb (no ib_async needed)."""
    ib = MockIb()
    with patch(_IB_PATH, return_value=ib):
        t = IbGatewayTransport(IbConnectionSpec())
    t._error_router.set_connected(True)
    t._mock_ib = ib  # type: ignore[attr-defined]
    yield t


@pytest.fixture
def router() -> _ErrorRouter:
    return _ErrorRouter()


def _req_waiter(router: _ErrorRouter, req_id: int = 1) -> asyncio.Future[Any]:
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    router._pending_request_waiters[req_id] = fut
    return fut


def _order_waiter(router: _ErrorRouter, order_id: int = 1) -> asyncio.Future[Any]:
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    router._pending_order_waiters[order_id] = fut
    return fut


def _record_states(transport: IbGatewayTransport) -> list[str]:
    states: list[str] = []
    transport.subscribe_connection_state(lambda event: states.append(event.state))
    return states


# --------------------------------------------------------------------------- C1


class TestC1FarmStatus:
    """C1: farm codes update stale_farms and publish FarmStatusEvent."""

    def test_c1_topic_name(self) -> None:
        assert FARM_TOPIC == "farm"

    def test_c1_no_stale_farms_initially(self, transport: IbGatewayTransport) -> None:
        assert transport.stale_farms == frozenset()

    @pytest.mark.parametrize(("code", "farm"), _FARM_BROKEN)
    def test_c1_broken_code_marks_farm_stale_and_publishes(
        self, transport: IbGatewayTransport, code: int, farm: str
    ) -> None:
        events: list[FarmStatusEvent] = []
        transport.subscribe_farm_status(events.append)
        transport._error_router.on_error(-1, code, "farm connection is broken")
        assert transport.stale_farms == frozenset({farm})
        assert isinstance(transport.stale_farms, frozenset)
        assert events == [FarmStatusEvent(farm, False, code, "farm connection is broken")]

    @pytest.mark.parametrize(
        ("bad", "ok", "farm"),
        [(b, o, f) for (b, f), (o, f2) in zip(_FARM_BROKEN, _FARM_OK, strict=True) if f == f2],
    )
    def test_c1_ok_code_clears_farm_and_publishes(
        self, transport: IbGatewayTransport, bad: int, ok: int, farm: str
    ) -> None:
        events: list[FarmStatusEvent] = []
        transport.subscribe_farm_status(events.append)
        transport._error_router.on_error(-1, bad, "broken")
        transport._error_router.on_error(-1, ok, "is OK")
        assert transport.stale_farms == frozenset()
        assert events[-1] == FarmStatusEvent(farm, True, ok, "is OK")
        assert len(events) == 2

    def test_c1_farms_tracked_independently(self, transport: IbGatewayTransport) -> None:
        for code in (2103, 2105, 2157):
            transport._error_router.on_error(-1, code, "broken")
        assert transport.stale_farms == frozenset({"market", "hmds", "secdef"})
        transport._error_router.on_error(-1, 2106, "ok")
        assert transport.stale_farms == frozenset({"market", "secdef"})

    def test_c1_ok_without_prior_break_is_harmless(self, transport: IbGatewayTransport) -> None:
        transport._error_router.on_error(-1, 2104, "ok")
        assert transport.stale_farms == frozenset()

    def test_c1_handle_close_unsubscribes(self, transport: IbGatewayTransport) -> None:
        events: list[FarmStatusEvent] = []
        handle = transport.subscribe_farm_status(events.append)
        assert isinstance(handle, SubscriptionHandle)
        transport._error_router.on_error(-1, 2103, "broken")
        handle.close()
        transport._error_router.on_error(-1, 2105, "broken")
        assert len(events) == 1

    @pytest.mark.parametrize("code", [c for c, _ in _FARM_BROKEN + _FARM_OK])
    async def test_c1_farm_codes_never_fail_waiters_or_flip_connection(
        self, transport: IbGatewayTransport, code: int
    ) -> None:
        router = transport._error_router
        req = _req_waiter(router, 1)
        order = _order_waiter(router, 2)
        states = _record_states(transport)
        router.on_error(1, code, "farm status")
        assert not req.done()
        assert not order.done()
        assert router.is_connected is True
        assert states == []


# --------------------------------------------------------------------------- C2/C3


class TestC2C3ConnectionLossCodes:
    """C2 (2110) and C3 (502, 504) behave like 1100."""

    @pytest.mark.parametrize("code", [2110, 502, 504])
    async def test_c2_c3_fail_all_pending_with_connection_lost(
        self, transport: IbGatewayTransport, code: int
    ) -> None:
        router = transport._error_router
        req = _req_waiter(router, 1)
        order = _order_waiter(router, 2)
        router.on_error(-1, code, "connectivity problem")
        assert router.is_connected is False
        for fut in (req, order):
            assert fut.done()
            assert isinstance(fut.exception(), ConnectionLostError)

    @pytest.mark.parametrize("code", [2110, 502, 504])
    def test_c2_c3_publish_disconnected(self, transport: IbGatewayTransport, code: int) -> None:
        states = _record_states(transport)
        transport._error_router.on_error(-1, code, "connectivity problem")
        assert states == ["disconnected"]

    @pytest.mark.parametrize("code", [2110, 502, 504])
    def test_c2_c3_router_listener_notified_false(self, router: _ErrorRouter, code: int) -> None:
        seen: list[bool] = []
        router._connection_listeners.append(seen.append)
        router.on_error(-1, code, "lost")
        assert seen == [False]

    def test_c2_1102_after_2110_restores_connected(self, transport: IbGatewayTransport) -> None:
        states = _record_states(transport)
        transport._error_router.on_error(-1, 2110, "lost")
        transport._error_router.on_error(-1, 1102, "restored")
        assert transport._error_router.is_connected is True
        assert states == ["disconnected", "connected"]

    def test_c2_1101_after_2110_restores_and_resubscribes(
        self, transport: IbGatewayTransport
    ) -> None:
        ib: MockIb = transport._mock_ib  # type: ignore[attr-defined]
        states = _record_states(transport)
        transport.subscribe("depth", object(), lambda _t: None)
        before = len(ib.req_mkt_depth_calls)
        transport._error_router.on_error(-1, 2110, "lost")
        transport._error_router.on_error(-1, 1101, "restored, data lost")
        assert transport._error_router.is_connected is True
        assert states == ["disconnected", "connected", "data_lost"]
        assert len(ib.req_mkt_depth_calls) == before + 1


# --------------------------------------------------------------------------- C4


class TestC4CompetingSession:
    """C4: 10197 fails the request waiter with MarketDataPermissionError mentioning competing."""

    async def test_c4_competing_session(self, router: _ErrorRouter) -> None:
        fut = _req_waiter(router, 7)
        router.on_error(7, 10197, "No market data during competing live session")
        exc = fut.exception()
        assert isinstance(exc, MarketDataPermissionError)
        assert "competing" in str(exc).lower()

    async def test_c4_competing_mentioned_even_if_ib_text_omits_it(
        self, router: _ErrorRouter
    ) -> None:
        fut = _req_waiter(router, 7)
        router.on_error(7, 10197, "market data unavailable")
        assert "competing" in str(fut.exception()).lower()

    def test_c4_no_waiter_does_not_raise(self, router: _ErrorRouter) -> None:
        router.on_error(7, 10197, "competing live session")


# --------------------------------------------------------------------------- C5


class TestC5Code162AndRateLimits:
    """C5: 162 disambiguation by text; 420 and 100 are rate limits."""

    async def test_c5_162_pacing_violation_is_rate_limit(self, router: _ErrorRouter) -> None:
        fut = _req_waiter(router)
        router.on_error(1, 162, "Historical Market Data Service error: Pacing Violation")
        exc = fut.exception()
        assert isinstance(exc, RateLimitError)
        assert isinstance(exc.retry_after, float)
        assert exc.retry_after > 0

    async def test_c5_162_no_market_data_permissions(self, router: _ErrorRouter) -> None:
        fut = _req_waiter(router)
        router.on_error(1, 162, "HMDS: No Market Data Permissions for NASDAQ STK")
        assert isinstance(fut.exception(), MarketDataPermissionError)

    @pytest.mark.parametrize(
        "text",
        [
            "Historical Market Data Service error message:HMDS query returned no data",
            "Historical data request timed out",
            "",
        ],
    )
    async def test_c5_162_other_text_is_historical_error(
        self, router: _ErrorRouter, text: str
    ) -> None:
        fut = _req_waiter(router)
        router.on_error(1, 162, text)
        exc = fut.exception()
        assert isinstance(exc, HistoricalDataError)
        assert not isinstance(exc, ContractNotFoundError)

    @pytest.mark.parametrize("code", [420, 100])
    async def test_c5_rate_limit_codes(self, router: _ErrorRouter, code: int) -> None:
        fut = _req_waiter(router)
        router.on_error(1, code, "Max rate of messages per second exceeded")
        exc = fut.exception()
        assert isinstance(exc, RateLimitError)
        assert exc.error_code == code
        assert isinstance(exc.retry_after, float)
        assert exc.retry_after > 0

    @pytest.mark.parametrize(
        "code,text",
        [
            (162, "Pacing violation"),
            (162, "No market data permissions"),
            (162, "no data"),
            (420, "pacing"),
            (100, "rate"),
        ],
    )
    async def test_c5_routes_to_request_waiter_only(
        self, router: _ErrorRouter, code: int, text: str
    ) -> None:
        req = _req_waiter(router, 5)
        other = _req_waiter(router, 6)
        order = _order_waiter(router, 5)
        router.on_error(5, code, text)
        assert req.done()
        assert not other.done()
        assert not order.done()
        assert 5 not in router._pending_request_waiters

    @pytest.mark.parametrize("code", [162, 420, 100])
    def test_c5_no_waiter_does_not_raise(self, router: _ErrorRouter, code: int) -> None:
        router.on_error(9, code, "whatever")


# --------------------------------------------------------------------------- C6


class TestC6SubscriptionLimit:
    """C6: 309 fails the request waiter with SubscriptionLimitError."""

    async def test_c6_waiter_fails(self, router: _ErrorRouter) -> None:
        fut = _req_waiter(router, 3)
        router.on_error(3, 309, "Max number (3) of market depth requests has been reached")
        exc = fut.exception()
        assert isinstance(exc, SubscriptionLimitError)
        assert exc.error_code == 309

    def test_c6_no_waiter_logs_warning(
        self, router: _ErrorRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            router.on_error(3, 309, "Max number of market depth requests")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("309" in r.getMessage() for r in warnings)


# --------------------------------------------------------------------------- C7


class TestC7OrderCancelled:
    """C7: 202 fails the order waiter with OrderCancelledError and records the reason."""

    async def test_c7_router_waiter_fails_with_reason(self, router: _ErrorRouter) -> None:
        fut = _order_waiter(router, 12)
        router.on_error(12, 202, "Order Canceled - reason:Insufficient margin")
        exc = fut.exception()
        assert isinstance(exc, OrderCancelledError)
        assert exc.reason == "Order Canceled - reason:Insufficient margin"
        assert 12 not in router._pending_order_waiters

    async def test_c7_does_not_touch_request_waiters(self, router: _ErrorRouter) -> None:
        req = _req_waiter(router, 12)
        router.on_error(12, 202, "Order Canceled - reason:")
        assert not req.done()

    async def test_c7_transport_waiter_and_cancel_reason(
        self, transport: IbGatewayTransport
    ) -> None:
        fut = _order_waiter(transport._error_router, 12)
        transport._error_router.on_error(12, 202, "Order Canceled - reason:Too far from market")
        exc = fut.exception()
        assert isinstance(exc, OrderCancelledError)
        assert exc.reason == "Order Canceled - reason:Too far from market"
        assert transport.cancel_reason(12) == "Order Canceled - reason:Too far from market"

    def test_c7_cancel_reason_recorded_without_waiter(self, transport: IbGatewayTransport) -> None:
        transport._error_router.on_error(40, 202, "Order Canceled - reason:Market closed")
        assert transport.cancel_reason(40) == "Order Canceled - reason:Market closed"

    def test_c7_cancel_reason_unknown_id_is_none(self, transport: IbGatewayTransport) -> None:
        transport._error_router.on_error(40, 202, "reason")
        assert transport.cancel_reason(41) is None
        assert transport.cancel_reason(-1) is None

    def test_c7_cancel_reason_tracks_each_order(self, transport: IbGatewayTransport) -> None:
        transport._error_router.on_error(1, 202, "first")
        transport._error_router.on_error(2, 202, "second")
        assert transport.cancel_reason(1) == "first"
        assert transport.cancel_reason(2) == "second"


# --------------------------------------------------------------------------- C8


@pytest.fixture
def _ib_async_stub() -> Iterator[MagicMock]:
    """Stub ib_async so the lazy ``from ib_async import Order`` works without the package."""
    stub = MagicMock()
    with patch.dict(sys.modules, {"ib_async": stub}):
        yield stub


@pytest.fixture
def hb_order() -> Any:
    from market_connector.orders import HBOrder, OrderType, TradeType  # noqa: PLC0415

    return HBOrder(
        order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal("10"), price=None
    )


class TestC8AdvancedReject:
    """C8: OrderRejectedError.advanced_reject comes from trade.advancedError."""

    def test_c8_default_is_none(self) -> None:
        assert OrderRejectedError(201, "rejected").advanced_reject is None

    def test_c8_kwarg_stored(self) -> None:
        exc = OrderRejectedError(201, "rejected", advanced_reject="detail")
        assert exc.advanced_reject == "detail"

    @pytest.mark.parametrize(
        ("advanced", "expected"),
        [("Rejected: price outside band", "Rejected: price outside band"), ("", None)],
    )
    async def test_c8_place_order_rejection(
        self,
        transport: IbGatewayTransport,
        hb_order: Any,
        _ib_async_stub: MagicMock,
        advanced: str,
        expected: str | None,
    ) -> None:
        trade = _make_trade(order_id=55, status="PendingSubmit")
        trade.advancedError = advanced
        transport._mock_ib.placeOrder = MagicMock(return_value=trade)  # type: ignore[attr-defined]

        async def fire_error() -> None:
            await asyncio.sleep(0.01)
            transport._error_router.on_error(55, 201, "Order rejected")

        task = asyncio.create_task(fire_error())
        with pytest.raises(OrderRejectedError) as info:
            await transport.place_order(MagicMock(), hb_order)
        await task
        assert info.value.advanced_reject == expected


# --------------------------------------------------------------------------- C9


def _unrouted(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "unrouted" in r.getMessage().lower()]


class TestC9Regression:
    """C9: unknown codes stay unrouted; existing mappings are unchanged."""

    @pytest.mark.parametrize("code", [2119, 399])
    def test_c9_unknown_code_logged_debug_not_raised(
        self, router: _ErrorRouter, caplog: pytest.LogCaptureFixture, code: int
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            router.on_error(1, code, "informational")
        records = _unrouted(caplog)
        assert any(str(code) in r.getMessage() for r in records)
        assert all(r.levelno == logging.DEBUG for r in records)

    @pytest.mark.parametrize("code", _NEW_CODES)
    def test_c9_new_codes_not_logged_as_unrouted(
        self, router: _ErrorRouter, caplog: pytest.LogCaptureFixture, code: int
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            router.on_error(1, code, "text")
        assert _unrouted(caplog) == []

    @pytest.mark.parametrize(
        ("code", "exc_cls"),
        [
            (165, HistoricalDataError),
            (200, ContractNotFoundError),
            (354, MarketDataPermissionError),
            (10089, MarketDataPermissionError),
        ],
    )
    async def test_c9_request_waiter_mappings(
        self, router: _ErrorRouter, code: int, exc_cls: type[Exception]
    ) -> None:
        fut = _req_waiter(router)
        router.on_error(1, code, "text")
        assert type(fut.exception()) is exc_cls

    @pytest.mark.parametrize("code", [201, 321, 325])
    async def test_c9_order_waiter_mappings(self, router: _ErrorRouter, code: int) -> None:
        fut = _order_waiter(router)
        router.on_error(1, code, "text")
        assert type(fut.exception()) is OrderRejectedError

    async def test_c9_1100_still_connection_lost(self, router: _ErrorRouter) -> None:
        router.set_connected(True)
        fut = _req_waiter(router)
        router.on_error(-1, 1100, "lost")
        assert router.is_connected is False
        assert type(fut.exception()) is ConnectionLostError

    def test_c9_1102_still_restores(self, router: _ErrorRouter) -> None:
        router.on_error(-1, 1102, "restored")
        assert router.is_connected is True

    async def test_c9_1300_still_terminated_without_flip(self, router: _ErrorRouter) -> None:
        router.set_connected(True)
        fut = _req_waiter(router)
        router.on_error(-1, 1300, "terminated")
        assert router.is_connected is True
        assert type(fut.exception()) is ConnectionTerminatedError

    async def test_c9_10090_still_warns_only(
        self, router: _ErrorRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        fut = _req_waiter(router)
        with caplog.at_level(logging.WARNING):
            router.on_error(1, 10090, "partially unavailable")
        assert not fut.done()
        assert any("10090" in r.getMessage() for r in caplog.records)
