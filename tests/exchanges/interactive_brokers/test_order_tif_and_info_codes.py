"""Acceptance tests for issue #71: explicit order TIF and informational IB order codes.

IB answers a MKT order without TIF with informational error 10349 ("Order TIF was
set to DAY based on order preset."). ib_async does not treat 10349 as a warning, so
it locally flips the Trade to status "Cancelled" (permId 0, a TradeLogEntry with
errorCode=10349) even though IB goes on to fill the order. These tests pin:

* orders carry an explicit ``tif`` (default "DAY");
* a cancel synthesised from an informational code is never reported as CANCELLED;
* 10349 followed by a fill ends FILLED;
* a genuine IB cancellation still ends CANCELLED.
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

from market_connector.exchanges.interactive_brokers.exceptions import OrderCancelledError
from market_connector.exchanges.interactive_brokers.order_handle import (
    IBOrderState,
    OrderHandle,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    FillEvent,
    IbGatewayTransport,
    _hb_to_ib_order,
)
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import FakeEvent, MockIb, make_fill

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_INFO_MSG = "Order TIF was set to DAY based on order preset."
_ORDER_ID = 7


@pytest.fixture(autouse=True)
def _ib_async_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ib_async.Order`` when ib_async is not installed (e.g. CI py312)."""
    if importlib.util.find_spec("ib_async") is None:
        stub = types.ModuleType("ib_async")
        stub.Order = MagicMock  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ib_async", stub)


def _market_order() -> HBOrder:
    return HBOrder(order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(10))


def _limit_order() -> HBOrder:
    return HBOrder(
        order_type=OrderType.LIMIT, side=TradeType.SELL, amount=Decimal(5), price=Decimal("101.5")
    )


class FakeTrade(SimpleNamespace):
    """ib_async.Trade lookalike: order, orderStatus, log, statusEvent, cancelledEvent."""

    def set_status(self, status: str, *, perm_id: int | None = None, filled: float = 0.0) -> None:
        self.orderStatus.status = status
        self.orderStatus.filled = filled
        self.orderStatus.avgFillPrice = 101.25 if filled else 0.0
        if perm_id is not None:
            self.order.permId = perm_id

    def ib_async_synthesised_cancel(self, code: int, msg: str) -> None:
        """What ib_async's wrapper does for a non-warning order error: local Cancelled."""
        self.set_status("Cancelled")
        self.log.append(SimpleNamespace(status="Cancelled", message=msg, errorCode=code))
        self.statusEvent(self)
        self.cancelledEvent(self)


def _make_trade() -> FakeTrade:
    return FakeTrade(
        order=SimpleNamespace(orderId=_ORDER_ID, permId=0, tif="", clientId=0),
        orderStatus=SimpleNamespace(status="PendingSubmit", filled=0.0, avgFillPrice=0.0),
        log=[],
        advancedError="",
        statusEvent=FakeEvent(),
        cancelledEvent=FakeEvent(),
    )


class OrderIb(MockIb):
    """MockIb that records placeOrder and hands back a scripted FakeTrade."""

    def __init__(self) -> None:
        super().__init__()
        self.trade = _make_trade()
        self.placed: list[Any] = []

    def placeOrder(self, contract: object, order: Any) -> FakeTrade:  # noqa: N802
        self.placed.append(order)
        self.trade.order.tif = getattr(order, "tif", "")
        return self.trade


async def _connected() -> tuple[IbGatewayTransport, OrderIb, list[FillEvent]]:
    ib = OrderIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec())
    transport._first_status_timeout = 2.0
    fills: list[FillEvent] = []
    transport.subscribe_fills(fills.append)
    await transport.connect()
    return transport, ib, fills


async def _settle() -> None:
    await asyncio.sleep(0.02)


class TestExplicitTif:
    @pytest.mark.parametrize("make_order", [_market_order, _limit_order])
    def test_built_order_defaults_to_day(self, make_order: Any) -> None:
        assert _hb_to_ib_order(make_order()).tif == "DAY"

    @pytest.mark.parametrize("make_order", [_market_order, _limit_order])
    async def test_order_sent_to_ib_carries_day_tif(self, make_order: Any) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), make_order()))
        await _settle()
        ib.trade.set_status("Submitted", perm_id=99)
        ib.trade.statusEvent(ib.trade)
        await task
        assert [o.tif for o in ib.placed] == ["DAY"]


class TestInformationalCancelIsNotACancel:
    async def test_10349_synthetic_cancel_keeps_order_pending(self) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        await _settle()
        assert not task.done(), "synthetic cancel from 10349 must not resolve place_order"

        ib.trade.set_status("Submitted", perm_id=99)
        ib.trade.statusEvent(ib.trade)
        handle = await task
        assert handle.status is IBOrderState.SUBMITTED

    async def test_10349_error_event_before_synthetic_cancel_also_ignored(self) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        ib.trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        await _settle()
        assert not task.done()

        ib.trade.set_status("PreSubmitted", perm_id=99)
        ib.trade.statusEvent(ib.trade)
        handle = await task
        assert handle.status is not IBOrderState.CANCELLED

    def test_handle_from_synthetically_cancelled_trade_is_not_cancelled(self) -> None:
        trade = _make_trade()
        trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        assert OrderHandle.from_trade(trade).status is not IBOrderState.CANCELLED


class TestInfoCodeThenFill:
    async def test_10349_then_fill_ends_filled(self) -> None:
        transport, ib, fills = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        await asyncio.sleep(0.05)

        ib.execDetailsEvent(ib.trade, make_fill("e1.01", order_id=_ORDER_ID, shares=10, cum_qty=10))
        ib.trade.set_status("Filled", perm_id=99, filled=10.0)
        ib.trade.statusEvent(ib.trade)

        handle = await task
        assert handle.status is IBOrderState.FILLED
        assert handle.filled_qty == Decimal(10)
        assert transport._handle_registry[_ORDER_ID].status is IBOrderState.FILLED
        assert [f.size for f in fills] == [Decimal(10)]

    async def test_10349_then_fill_emits_no_cancel_to_consumers(self) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        ib.trade.set_status("Filled", perm_id=99, filled=10.0)
        ib.trade.statusEvent(ib.trade)

        handle = await task
        assert handle.status is not IBOrderState.CANCELLED
        assert transport.cancel_reason(_ORDER_ID) is None
        assert all(
            h.status is not IBOrderState.CANCELLED for h in transport._handle_registry.values()
        )


class TestGenuineCancelStillCancelled:
    async def test_ib_reported_cancel_with_perm_id_is_cancelled(self) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.set_status("Cancelled", perm_id=99)
        ib.trade.log.append(SimpleNamespace(status="Cancelled", message="", errorCode=0))
        ib.trade.statusEvent(ib.trade)

        handle = await task
        assert handle.status is IBOrderState.CANCELLED

    async def test_error_202_order_canceled_is_still_a_cancel(self) -> None:
        transport, ib, _ = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.errorEvent(_ORDER_ID, 202, "Order Canceled - reason:", None)

        with pytest.raises(OrderCancelledError):
            await task
        assert transport.cancel_reason(_ORDER_ID) == "Order Canceled - reason:"
