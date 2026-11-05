"""Acceptance tests for issue #182: fractional-share (STK) orders on IB.

Contract (option a): fractional quantities are passed to IB exactly as given and IB
decides; when IB refuses, the caller gets ``OrderRejectedError`` carrying IB's code,
IB's message and the submitted quantity. Nothing is rejected locally.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import OrderRejectedError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1111111"

# Representative IB refusal of a fractional order sent through the API.
FRACTIONAL_REFUSAL_CODE = 10243
FRACTIONAL_REFUSAL_MSG = "Fractional-sized order cannot be placed via API for this contract."

FRACTIONAL_QUANTITIES = ["1.5", "0.0275"]
ALL_QUANTITIES = ["10", *FRACTIONAL_QUANTITIES]

ORDER_TYPES = [
    OrderType.MARKET,
    OrderType.LIMIT,
    OrderType.STOP_LOSS,
    OrderType.STOP_LOSS_LIMIT,
]


def _order(order_type: OrderType, amount: str, side: TradeType = TradeType.BUY) -> HBOrder:
    needs_price = order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT)
    needs_stop = order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT)
    return HBOrder(
        order_type=order_type,
        side=side,
        amount=Decimal(amount),
        price=Decimal("100.5") if needs_price else None,
        stop_price=Decimal("99.5") if needs_stop else None,
    )


async def _make() -> tuple[IbGatewayGateway, IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = 0.5
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=MagicMock(native=MagicMock(name="stk_contract")))
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, transport, ib


async def _when_placed(ib: MockIb, count: int = 1) -> None:
    for _ in range(500):
        if len(ib.placed_orders) >= count:
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"only {len(ib.placed_orders)} of {count} orders were placed")


async def _ack(ib: MockIb, count: int = 1) -> None:
    await _when_placed(ib, count)
    for order in ib.placed_orders[:count]:
        ib.emit_status(order.orderId, "Submitted")


async def _refuse(ib: MockIb) -> None:
    await _when_placed(ib)
    ib.emit_error(ib.placed_orders[0].orderId, FRACTIONAL_REFUSAL_CODE, FRACTIONAL_REFUSAL_MSG)


def _wire(order: Any) -> str:
    return str(order.totalQuantity)


class TestFractionalPassThrough:
    @pytest.mark.parametrize("amount", ALL_QUANTITIES)
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    async def test_place_order_sends_quantity_exactly(
        self, order_type: OrderType, amount: str
    ) -> None:
        gateway, _, ib = await _make()
        await asyncio.gather(_ack(ib), gateway.place_order(MagicMock(), _order(order_type, amount)))
        assert len(ib.placed_orders) == 1
        assert _wire(ib.placed_orders[0]) == amount

    @pytest.mark.parametrize("amount", ALL_QUANTITIES)
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    async def test_what_if_sends_quantity_exactly(self, order_type: OrderType, amount: str) -> None:
        gateway, _, ib = await _make()
        await gateway.what_if(MagicMock(), _order(order_type, amount))
        assert len(ib.what_if_calls) == 1
        assert _wire(ib.what_if_calls[0][1]) == amount

    @pytest.mark.parametrize("amount", ALL_QUANTITIES)
    async def test_bracket_legs_send_quantity_exactly(self, amount: str) -> None:
        gateway, _, ib = await _make()
        entry = _order(OrderType.LIMIT, amount)
        take_profit = HBOrder(
            order_type=OrderType.LIMIT,
            side=TradeType.SELL,
            amount=Decimal(amount),
            price=Decimal("110"),
        )
        stop_loss = HBOrder(
            order_type=OrderType.STOP_LOSS,
            side=TradeType.SELL,
            amount=Decimal(amount),
            stop_price=Decimal("95"),
        )
        await asyncio.gather(
            _ack(ib, 3), gateway.place_bracket(MagicMock(), entry, take_profit, stop_loss)
        )
        assert [_wire(o) for o in ib.placed_orders] == [amount] * 3


class TestFractionalRejection:
    @pytest.mark.parametrize("amount", FRACTIONAL_QUANTITIES)
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    async def test_ib_refusal_raises_order_rejected_with_details(
        self, order_type: OrderType, amount: str
    ) -> None:
        gateway, _, ib = await _make()
        results = await asyncio.gather(
            _refuse(ib),
            gateway.place_order(MagicMock(), _order(order_type, amount)),
            return_exceptions=True,
        )
        exc = results[1]
        assert isinstance(exc, OrderRejectedError), f"got {exc!r}"
        assert exc.error_code == FRACTIONAL_REFUSAL_CODE
        assert exc.message == FRACTIONAL_REFUSAL_MSG
        assert exc.quantity == amount  # type: ignore[attr-defined]

    async def test_refusal_is_prompt_not_a_timeout(self) -> None:
        gateway, transport, ib = await _make()
        transport._first_status_timeout = 5.0
        results = await asyncio.wait_for(
            asyncio.gather(
                _refuse(ib),
                gateway.place_order(MagicMock(), _order(OrderType.MARKET, "1.5")),
                return_exceptions=True,
            ),
            timeout=1.0,
        )
        assert isinstance(results[1], OrderRejectedError), f"got {results[1]!r}"


class TestNoUpFrontRejection:
    @pytest.mark.parametrize("amount", FRACTIONAL_QUANTITIES)
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    async def test_fractional_never_raises_value_error_locally(
        self, order_type: OrderType, amount: str
    ) -> None:
        # Default trading rules report size_increment 1; IB, not the library, decides.
        gateway, _, ib = await _make()
        results = await asyncio.gather(
            _ack(ib),
            gateway.place_order(MagicMock(), _order(order_type, amount)),
            return_exceptions=True,
        )
        assert not isinstance(results[1], ValueError), f"got {results[1]!r}"
        assert len(ib.placed_orders) == 1

    @pytest.mark.parametrize("amount", FRACTIONAL_QUANTITIES)
    async def test_what_if_never_raises_value_error_locally(self, amount: str) -> None:
        gateway, _, ib = await _make()
        await gateway.what_if(MagicMock(), _order(OrderType.LIMIT, amount))
        assert len(ib.what_if_calls) == 1
