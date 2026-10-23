"""Acceptance tests for issue #83: IB warning 399 must not break order status.

ib_async sets ``orderStatus.status = "ValidationError"`` on a trade for a *warning*
(e.g. 399 "will not be placed until market open"). The order is still live at IB
(permId set), so it must map to SUBMITTED (or PARTIALLY_FILLED when partly filled),
never raise, and never be dropped from open orders or time out ``place_order``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.order_handle import (
    IBOrderState,
    OrderHandle,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import FakeEvent, MockIb

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_WARNING_399 = (
    "Warning 399, reqId 5: Order Message: BUY 1 AAPL NASDAQ.NMS Warning: Your order "
    "will not be placed at the exchange until 2026-10-23 09:30:00 US/Eastern."
)
_INFO_MSG = "Order TIF was set to DAY based on order preset."
_ORDER_ID = 7
_PERM_ID = 99


@pytest.fixture(autouse=True)
def _ib_async_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ib_async.Order`` when ib_async is not installed (e.g. CI py312)."""
    if importlib.util.find_spec("ib_async") is None:
        stub = types.ModuleType("ib_async")
        stub.Order = MagicMock  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ib_async", stub)


def _limit_order() -> HBOrder:
    return HBOrder(
        order_type=OrderType.LIMIT,
        side=TradeType.BUY,
        amount=Decimal(1),
        price=Decimal(150),
    )


def _market_order() -> HBOrder:
    return HBOrder(order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(10))


class FakeTrade(SimpleNamespace):
    """ib_async.Trade lookalike: order, orderStatus, log, statusEvent, cancelledEvent."""

    def set_status(
        self,
        status: str,
        *,
        perm_id: int | None = None,
        filled: float = 0.0,
        remaining: float = 1.0,
        code: int = 0,
        message: str = "",
    ) -> None:
        self.orderStatus.status = status
        self.orderStatus.filled = filled
        self.orderStatus.remaining = remaining
        self.orderStatus.avgFillPrice = 150.0 if filled else 0.0
        if perm_id is not None:
            self.order.permId = perm_id
        self.log.append(SimpleNamespace(status=status, message=message, errorCode=code))

    def emit(self, status: str, **kwargs: Any) -> None:
        self.set_status(status, **kwargs)
        self.statusEvent(self)

    def validation_warning_399(self, **kwargs: Any) -> None:
        """What ib_async's wrapper does for warning 399: status=ValidationError + log."""
        self.set_status("ValidationError", code=399, message=_WARNING_399, **kwargs)
        self.statusEvent(self)


def _make_trade() -> FakeTrade:
    return FakeTrade(
        order=SimpleNamespace(orderId=_ORDER_ID, permId=0, tif="", clientId=0),
        orderStatus=SimpleNamespace(
            status="PendingSubmit", filled=0.0, remaining=1.0, avgFillPrice=0.0
        ),
        log=[],
        advancedError="",
        statusEvent=FakeEvent(),
        cancelledEvent=FakeEvent(),
    )


def _validation_error_trade(*, filled: float = 0.0, remaining: float = 1.0) -> FakeTrade:
    trade = _make_trade()
    trade.set_status("PendingSubmit")
    trade.set_status("PreSubmitted", perm_id=_PERM_ID, filled=filled, remaining=remaining)
    trade.set_status(
        "ValidationError",
        perm_id=_PERM_ID,
        filled=filled,
        remaining=remaining,
        code=399,
        message=_WARNING_399,
    )
    return trade


class OrderIb(MockIb):
    """MockIb that records placeOrder and hands back a scripted FakeTrade."""

    def __init__(self) -> None:
        super().__init__()
        self.trade = _make_trade()

    def placeOrder(self, contract: object, order: Any) -> FakeTrade:  # noqa: N802
        self.trade.order.tif = getattr(order, "tif", "")
        return self.trade

    def openTrades(self) -> list[FakeTrade]:  # noqa: N802
        return [self.trade]

    async def reqAllOpenOrdersAsync(self) -> list[object]:  # noqa: N802
        return []


async def _connected(first_status_timeout: float = 2.0) -> tuple[IbGatewayTransport, OrderIb]:
    ib = OrderIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec())
    transport._first_status_timeout = first_status_timeout
    await transport.connect()
    return transport, ib


async def _settle() -> None:
    await asyncio.sleep(0.02)


class TestFromTradeValidationError:
    def test_validation_error_399_maps_submitted(self) -> None:
        handle = OrderHandle.from_trade(_validation_error_trade())
        assert handle.status is IBOrderState.SUBMITTED
        assert handle.raw_status == "ValidationError"

    def test_validation_error_with_partial_fill_maps_partially_filled(self) -> None:
        handle = OrderHandle.from_trade(_validation_error_trade(filled=1.0, remaining=2.0))
        assert handle.status is IBOrderState.PARTIALLY_FILLED


class TestPlaceOrderValidationError:
    async def test_presubmitted_then_validation_error_returns_submitted(self) -> None:
        transport, ib = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _limit_order()))
        await _settle()

        ib.trade.emit("PendingSubmit")
        await _settle()
        # Both events arrive in one socket batch: the waiter resumes after the warning.
        ib.trade.emit("PreSubmitted", perm_id=_PERM_ID)
        ib.trade.validation_warning_399(perm_id=_PERM_ID)

        handle = await asyncio.wait_for(task, timeout=3.0)
        assert handle.status is IBOrderState.SUBMITTED

    async def test_first_ack_is_validation_error_returns_submitted(self) -> None:
        transport, ib = await _connected(first_status_timeout=0.5)
        task = asyncio.create_task(transport.place_order(MagicMock(), _limit_order()))
        await _settle()

        ib.trade.emit("PendingSubmit")
        await _settle()
        ib.trade.validation_warning_399(perm_id=_PERM_ID)

        handle = await asyncio.wait_for(task, timeout=3.0)
        assert handle.status is IBOrderState.SUBMITTED


class TestOpenOrdersValidationError:
    async def test_gateway_open_orders_includes_validation_error_as_submitted(self) -> None:
        transport, ib = await _connected()
        ib.trade = _validation_error_trade()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())

        handles = gateway.get_open_orders()

        assert [(h.order_id, h.status) for h in handles] == [(_ORDER_ID, IBOrderState.SUBMITTED)]


class TestGuards:
    async def test_71_synthetic_cancel_10349_still_pending(self) -> None:
        transport, ib = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.set_status("Cancelled", code=10349, message=_INFO_MSG)
        ib.trade.statusEvent(ib.trade)
        ib.trade.cancelledEvent(ib.trade)
        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        await _settle()

        assert OrderHandle.from_trade(ib.trade).status is IBOrderState.PENDING
        assert not task.done()
        task.cancel()

    def test_77_presubmitted_maps_submitted(self) -> None:
        trade = _make_trade()
        trade.set_status("PreSubmitted", perm_id=_PERM_ID)
        assert OrderHandle.from_trade(trade).status is IBOrderState.SUBMITTED

    def test_81_apipending_maps_pending(self) -> None:
        trade = _make_trade()
        trade.set_status("ApiPending")
        assert OrderHandle.from_trade(trade).status is IBOrderState.PENDING
