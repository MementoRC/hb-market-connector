"""Acceptance tests for issue #81: ApiPending must not be reported as SUBMITTED.

ApiPending, like PendingSubmit, means IB has not acknowledged the order yet. It must
map to PENDING and ``place_order`` must not resolve its waiter on it. A later
PreSubmitted/Submitted (permId set) resolves SUBMITTED (#77); a synthetic cancel from
an informational code still maps PENDING and does not resolve the waiter (#71).
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

from market_connector.exchanges.interactive_brokers.order_handle import (
    IBOrderState,
    OrderHandle,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import FakeEvent, MockIb

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
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
        remaining: float = 10.0,
    ) -> None:
        self.orderStatus.status = status
        self.orderStatus.filled = filled
        self.orderStatus.remaining = remaining
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
        order=SimpleNamespace(orderId=_ORDER_ID, permId=0, tif=""),
        orderStatus=SimpleNamespace(
            status="PendingSubmit", filled=0.0, remaining=10.0, avgFillPrice=0.0
        ),
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

    def placeOrder(self, contract: object, order: Any) -> FakeTrade:  # noqa: N802
        self.trade.order.tif = getattr(order, "tif", "")
        return self.trade


async def _connected(first_status_timeout: float = 2.0) -> tuple[IbGatewayTransport, OrderIb]:
    ib = OrderIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec())
    transport._first_status_timeout = first_status_timeout
    await transport.connect()
    return transport, ib


async def _settle() -> None:
    await asyncio.sleep(0.02)


class TestApiPendingFromTrade:
    def test_apipending_is_pending(self) -> None:
        trade = _make_trade()
        trade.set_status("ApiPending")
        assert OrderHandle.from_trade(trade).status is IBOrderState.PENDING


class TestPlaceOrderApiPending:
    async def test_apipending_then_submitted_returns_submitted(self) -> None:
        transport, ib = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.set_status("ApiPending")
        ib.trade.statusEvent(ib.trade)
        await _settle()
        assert not task.done(), "ApiPending must not resolve place_order"
        ib.trade.set_status("Submitted", perm_id=_PERM_ID)
        ib.trade.statusEvent(ib.trade)

        handle = await asyncio.wait_for(task, timeout=3.0)
        assert handle.status is IBOrderState.SUBMITTED

    async def test_pendingsubmit_then_presubmitted_still_returns_submitted(self) -> None:
        transport, ib = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.set_status("PendingSubmit")
        ib.trade.statusEvent(ib.trade)
        await _settle()
        ib.trade.set_status("PreSubmitted", perm_id=_PERM_ID)
        ib.trade.statusEvent(ib.trade)

        handle = await asyncio.wait_for(task, timeout=3.0)
        assert handle.status is IBOrderState.SUBMITTED

    async def test_apipending_only_ends_on_first_status_timeout(self) -> None:
        transport, ib = await _connected(first_status_timeout=0.2)
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.set_status("ApiPending")
        ib.trade.statusEvent(ib.trade)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3.0)


class TestSyntheticCancelStillPending:
    async def test_10349_synthetic_cancel_maps_pending_and_does_not_resolve(self) -> None:
        transport, ib = await _connected()
        task = asyncio.create_task(transport.place_order(MagicMock(), _market_order()))
        await _settle()

        ib.trade.ib_async_synthesised_cancel(10349, _INFO_MSG)
        ib.errorEvent(_ORDER_ID, 10349, _INFO_MSG, None)
        await _settle()

        assert OrderHandle.from_trade(ib.trade).status is IBOrderState.PENDING
        assert not task.done(), "synthetic cancel from 10349 must not resolve place_order"
        task.cancel()
