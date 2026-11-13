"""Acceptance tests for issue #203: OrderHandle perm_id / client_order_id accessors.

Red phase: the new properties are read inside test bodies so each test fails on its own
and collection never fails. ``order_id`` keeps its behaviour (permId if set, else orderId).
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU111"
OWN_CLIENT_ID = 5
OTHER_CLIENT_ID = 9


def _trade(
    order_id: int = 7,
    client_id: int = OWN_CLIENT_ID,
    *,
    perm_id: int = 0,
    symbol: str = "AAPL",
) -> Any:
    """Real ib_async Trade, as openTrades() serves them."""
    from ib_async import Contract, Order, OrderStatus, Trade  # noqa: PLC0415

    return Trade(
        contract=Contract(symbol=symbol),
        order=Order(
            orderId=order_id,
            clientId=client_id,
            permId=perm_id,
            action="BUY",
            totalQuantity=100.0,
        ),
        orderStatus=OrderStatus(orderId=order_id, status="Submitted"),
    )


def _place_order_trade(order_id: int, perm_id: int = 0) -> MagicMock:
    """MagicMock trade in the style of test_transport_orders.py::_make_trade."""
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.orderStatus.status = "Submitted"
    trade.orderStatus.filled = 0.0
    trade.orderStatus.avgFillPrice = 0.0
    handlers: list = []

    def iadd(_self: Any, cb: Any) -> Any:
        handlers.append(cb)
        return trade.statusEvent

    def isub(_self: Any, cb: Any) -> Any:
        if cb in handlers:
            handlers.remove(cb)
        return trade.statusEvent

    trade.statusEvent.__iadd__ = iadd
    trade.statusEvent.__isub__ = isub
    trade.statusEvent.side_effect = lambda *a: [h(*a) for h in list(handlers)]
    return trade


async def _connected_transport(ib: MockIb) -> IbGatewayTransport:
    ib.managed_accounts = ["DU111", "DU222"]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT, client_id=OWN_CLIENT_ID))
    await transport.connect()
    return transport


async def _sync(transport: IbGatewayTransport, ib: MockIb, trades: list[Any]) -> None:
    cache: list[Any] = []

    async def fake_req_all() -> None:
        cache[:] = trades

    ib.openTrades = lambda: list(cache)  # type: ignore[method-assign]
    ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=fake_req_all)
    await transport.sync_open_orders(timeout=2.0)


class TestIdAccessors:
    def test_perm_id_assigned(self) -> None:
        handle = OrderHandle.from_trade(_trade(order_id=7, perm_id=123456))
        assert handle.perm_id == 123456
        assert handle.client_order_id == 7
        assert handle.order_id == 123456  # unchanged: permId wins

    def test_before_perm_id_assigned(self) -> None:
        handle = OrderHandle.from_trade(_trade(order_id=7, perm_id=0))
        assert handle.perm_id is None
        assert handle.client_order_id == 7
        assert handle.order_id == 7  # unchanged: falls back to orderId

    @pytest.mark.parametrize("name", ["perm_id", "client_order_id"])
    def test_assignment_raises(self, name: str) -> None:
        handle = OrderHandle.from_trade(_trade(perm_id=99))
        assert getattr(handle, name) is not None  # exists first; else the raise is vacuous
        with pytest.raises(AttributeError):
            setattr(handle, name, 1)


class TestOriginCoverage:
    async def test_place_order_handle_exposes_client_order_id(self) -> None:
        trade = _place_order_trade(order_id=42)
        ib = MagicMock()
        ib.connectAsync = AsyncMock()
        ib.isConnected = MagicMock(return_value=True)
        ib.errorEvent = MagicMock()
        ib.errorEvent.__iadd__ = MagicMock()
        ib.errorEvent.__isub__ = MagicMock()
        ib.placeOrder = MagicMock(return_value=trade)

        from market_connector.orders import HBOrder, OrderType, TradeType  # noqa: PLC0415

        order = HBOrder(
            order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(10), price=None
        )
        with (
            patch.dict(sys.modules, {"ib_async": MagicMock()}),
            patch(_IB_PATH, return_value=ib),
        ):
            t = IbGatewayTransport(
                IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)
            )
            t._error_router._is_connected = True

            async def delayed_fire() -> None:
                await asyncio.sleep(0.01)
                trade.statusEvent(trade)

            fire = asyncio.create_task(delayed_fire())
            handle = await t.place_order(MagicMock(), order)
            await fire

        assert handle.client_order_id == 42
        assert handle.perm_id is None

    async def test_sync_open_orders_foreign_and_manual_expose_both_ids(self) -> None:
        ib = MockIb()
        transport = await _connected_transport(ib)
        await _sync(
            transport,
            ib,
            [
                _trade(7, OTHER_CLIENT_ID, perm_id=555),
                _trade(-1, 0, perm_id=777, symbol="NVDA"),
            ],
        )

        handles = {h.perm_id: h for h in transport.open_orders()}
        other = handles[555]
        assert other.owned is False
        assert other.client_order_id == 7
        manual = handles[777]
        assert manual.manual is True
        assert manual.client_order_id == -1


class TestSubmitTimeMatching:
    async def test_match_recorded_handle_after_perm_id_assigned(self) -> None:
        # At submit time permId is unset, so order_id == orderId; the caller keys on it.
        submitted = OrderHandle.from_trade(_trade(order_id=7, perm_id=0), OWN_CLIENT_ID)
        recorded = {submitted.order_id: "qrl-order"}

        ib = MockIb()
        transport = await _connected_transport(ib)
        await _sync(transport, ib, [_trade(7, OWN_CLIENT_ID, perm_id=424242)])

        (later,) = transport.open_orders()
        assert later.order_id == 424242  # order_id moved to permId; key no longer matches
        assert later.order_id not in recorded
        assert recorded[later.client_order_id] == "qrl-order"
        assert later.perm_id == 424242
