"""Tests for transport.place_order — lifecycle, error race, registry, timeout."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    OrderRejectedError,
)
from market_connector.exchanges.interactive_brokers.order_handle import OrderState
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@pytest.fixture(autouse=True)
def _patch_ib_async_module():
    """Inject a MagicMock for ib_async so lazy imports inside transport succeed.

    ib_async is an optional extra not installed in the default pixi env.  Every
    call path exercised here goes through _hb_to_ib_order which does
    ``from ib_async import Order`` at call time.  Patching sys.modules satisfies
    that import without requiring the real package.
    """
    mock_ib_async = MagicMock()
    with patch.dict(sys.modules, {"ib_async": mock_ib_async}):
        yield mock_ib_async


def _make_spec() -> IbConnectionSpec:
    return IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)


def _make_trade(
    order_id: int = 1,
    perm_id: int = 0,
    status: str = "Submitted",
    filled: float = 0.0,
    avg_fill_price: float = 0.0,
) -> MagicMock:
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.avgFillPrice = avg_fill_price

    _handlers: list = []

    def iadd(_self, cb):
        _handlers.append(cb)
        return trade.statusEvent

    def isub(_self, cb):
        if cb in _handlers:
            _handlers.remove(cb)
        return trade.statusEvent

    def call(*args):
        for h in list(_handlers):
            h(*args)

    trade.statusEvent.__iadd__ = iadd
    trade.statusEvent.__isub__ = isub
    # side_effect is invoked by MagicMock.__call__, so calling
    # trade.statusEvent(trade) dispatches to all registered handlers.
    trade.statusEvent.side_effect = call
    trade._handlers = _handlers
    return trade


@pytest.fixture
def mock_ib() -> MagicMock:
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.disconnect = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    return ib


@pytest.fixture
def mock_hb_order() -> MagicMock:
    from market_connector.orders import HBOrder, OrderType, TradeType  # noqa: PLC0415

    return HBOrder(
        order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal("10"), price=None
    )


class TestPlaceOrderHappyPath:
    @pytest.mark.asyncio
    async def test_place_order_returns_submitted_handle(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        trade = _make_trade(order_id=42, status="Submitted")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            native = MagicMock()

            async def delayed_fire() -> None:
                await asyncio.sleep(0.01)
                trade.statusEvent(trade)

            asyncio.create_task(delayed_fire())
            handle = await t.place_order(native, mock_hb_order)

        assert handle.status == OrderState.SUBMITTED
        assert handle.order_id == 42

    @pytest.mark.asyncio
    async def test_place_order_registers_handle_in_registry(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        trade = _make_trade(order_id=7, status="Submitted")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            native = MagicMock()

            async def delayed_fire() -> None:
                await asyncio.sleep(0.01)
                trade.statusEvent(trade)

            asyncio.create_task(delayed_fire())
            await t.place_order(native, mock_hb_order)

        assert 7 in t._handle_registry


class TestPlaceOrderErrorRace:
    @pytest.mark.asyncio
    async def test_error_before_status_raises_order_rejected(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        """Error router fires OrderRejectedError before statusEvent — place_order raises."""
        trade = _make_trade(order_id=55, status="Submitted")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            native = MagicMock()

            async def fire_error() -> None:
                await asyncio.sleep(0.01)
                # Signature is req_id (snake_case), not reqId.
                t._error_router.on_error(req_id=55, code=201, msg="order rejected")

            asyncio.create_task(fire_error())

            with pytest.raises(OrderRejectedError):
                await t.place_order(native, mock_hb_order)

    @pytest.mark.asyncio
    async def test_not_connected_raises_connection_lost(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = False
            native = MagicMock()

            with pytest.raises(ConnectionLostError):
                await t.place_order(native, mock_hb_order)


class TestPlaceOrderTimeout:
    @pytest.mark.asyncio
    async def test_no_status_event_raises_timeout(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        trade = _make_trade(order_id=99, status="PendingSubmit")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            t._first_status_timeout = 0.05  # override for test speed
            native = MagicMock()

            with pytest.raises(asyncio.TimeoutError):
                await t.place_order(native, mock_hb_order)

    @pytest.mark.asyncio
    async def test_pending_submit_status_does_not_resolve_future(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        """A PendingSubmit statusEvent echo must be ignored; timeout must still fire."""
        trade = _make_trade(order_id=88, status="PendingSubmit")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            t._first_status_timeout = 0.05
            native = MagicMock()

            async def fire_pending_submit() -> None:
                await asyncio.sleep(0.01)
                # Fire PendingSubmit — should be ignored by _on_status.
                trade.statusEvent(trade)

            asyncio.create_task(fire_pending_submit())

            with pytest.raises(asyncio.TimeoutError):
                await t.place_order(native, mock_hb_order)


class TestPlaceOrderHandleOrderId:
    @pytest.mark.asyncio
    async def test_handle_uses_perm_id_when_nonzero(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        """OrderHandle.from_trade prefers permId over orderId when permId != 0."""
        trade = _make_trade(order_id=10, perm_id=9999, status="Submitted")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            native = MagicMock()

            async def delayed_fire() -> None:
                await asyncio.sleep(0.01)
                trade.statusEvent(trade)

            asyncio.create_task(delayed_fire())
            handle = await t.place_order(native, mock_hb_order)

        # Registry is keyed by orderId (10); handle.order_id is permId (9999).
        assert 10 in t._handle_registry
        assert handle.order_id == 9999

    @pytest.mark.asyncio
    async def test_handle_falls_back_to_order_id_when_perm_id_zero(
        self, mock_ib: MagicMock, mock_hb_order: MagicMock
    ) -> None:
        trade = _make_trade(order_id=21, perm_id=0, status="Submitted")
        mock_ib.placeOrder = MagicMock(return_value=trade)

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(_make_spec())
            t._error_router._is_connected = True
            native = MagicMock()

            async def delayed_fire() -> None:
                await asyncio.sleep(0.01)
                trade.statusEvent(trade)

            asyncio.create_task(delayed_fire())
            handle = await t.place_order(native, mock_hb_order)

        assert handle.order_id == 21
