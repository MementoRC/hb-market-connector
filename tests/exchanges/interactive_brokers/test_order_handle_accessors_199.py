"""Acceptance tests for issue #199: OrderHandle symbol/action/total_quantity accessors.

Red phase: the new properties are read inside test bodies so each test fails on its own
and collection never fails. Quantity is Decimal, matching ``filled_qty`` on the handle.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

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
    symbol: str = "AAPL",
    action: str = "BUY",
    total_quantity: float = 100.0,
) -> Any:
    """Real ib_async Trade, as openTrades() serves them."""
    from ib_async import Contract, Order, OrderStatus, Trade  # noqa: PLC0415

    return Trade(
        contract=Contract(symbol=symbol),
        order=Order(
            orderId=order_id,
            clientId=client_id,
            action=action,
            totalQuantity=total_quantity,
        ),
        orderStatus=OrderStatus(orderId=order_id, status="Submitted"),
    )


class TestOrderHandleAccessors:
    def test_symbol(self) -> None:
        handle = OrderHandle.from_trade(_trade(symbol="MSFT"))
        assert handle.symbol == "MSFT"

    @pytest.mark.parametrize("action", ["BUY", "SELL"])
    def test_action_is_ib_side_string(self, action: str) -> None:
        handle = OrderHandle.from_trade(_trade(action=action))
        assert handle.action == action

    def test_total_quantity_is_decimal(self) -> None:
        handle = OrderHandle.from_trade(_trade(total_quantity=12.5))
        assert handle.total_quantity == Decimal("12.5")
        assert isinstance(handle.total_quantity, Decimal)

    def test_accessors_survive_replace_of_handle_fields(self) -> None:
        from dataclasses import replace  # noqa: PLC0415

        handle = replace(OrderHandle.from_trade(_trade(symbol="TSLA")), owned=False)
        assert handle.symbol == "TSLA"


class TestOrderHandleAccessorsReadOnly:
    @pytest.mark.parametrize("name", ["symbol", "action", "total_quantity"])
    def test_assignment_raises(self, name: str) -> None:
        handle = OrderHandle.from_trade(_trade())
        assert getattr(handle, name) is not None  # exists first; else the raise is vacuous
        with pytest.raises(AttributeError):
            setattr(handle, name, "x")


class TestForeignOrderAccessors:
    async def test_sync_open_orders_exposes_foreign_and_manual_orders_by_name(self) -> None:
        ib = MockIb()
        ib.managed_accounts = ["DU111", "DU222"]
        with patch(_IB_PATH, return_value=ib):
            transport = IbGatewayTransport(
                IbConnectionSpec(account_id=ACCT, client_id=OWN_CLIENT_ID)
            )
        await transport.connect()

        trades = [
            _trade(7, OTHER_CLIENT_ID, symbol="AAPL", action="BUY", total_quantity=100.0),
            _trade(-1, 0, symbol="NVDA", action="SELL", total_quantity=25.0),
        ]
        cache: list[Any] = []

        async def fake_req_all() -> None:
            cache[:] = trades

        ib.openTrades = lambda: list(cache)  # type: ignore[method-assign]
        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=fake_req_all)
        await transport.sync_open_orders(timeout=2.0)

        handles = {h.order_id: h for h in transport.open_orders()}
        other = handles[7]
        assert other.owned is False
        assert (other.symbol, other.action, other.total_quantity) == (
            "AAPL",
            "BUY",
            Decimal(100),
        )
        manual = handles[-1]
        assert manual.manual is True
        assert (manual.symbol, manual.action, manual.total_quantity) == (
            "NVDA",
            "SELL",
            Decimal(25),
        )
