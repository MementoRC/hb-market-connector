"""RED acceptance tests for #250 (Interactive Brokers): the link tag is carried in ``orderRef``.

Placement mechanism pinned here (mirrors Coinbase's ``client_order_id`` kwarg for single orders
and Kraken's ``options={"group_id": n}`` for linked orders):

* ``IbGatewayGateway.place_order(ref, hb_order, link_tag="mcl-5-0")`` sets ``orderRef`` to the tag
  on the ib_async ``Order`` handed to ``placeOrder``.
* ``place_bracket(target, spec, options={"group_id": 5})`` tags the legs in
  ``LinkedOrderSpec.ordered_legs()`` order (entry, take-profit, stop-loss): ``mcl-5-0``,
  ``mcl-5-1``, ``mcl-5-2`` (a single exit is ``mcl-5-1``).
  ``place_oco(..., options={"group_id": 6})`` tags the legs ``mcl-6-0``, ``mcl-6-1`` in
  ``ordered_legs()`` order.
* Without a link tag / ``group_id`` option, ``orderRef`` stays empty (existing behaviour).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from data_type_primitives import (
    LegRole,
    LegSpec,
    LinkedOrderSpec,
    LinkKind,
    TriggerKind,
    TriggerSpec,
)
from data_type_primitives import OrderType as DtpOrderType
from data_type_primitives import TradeType as DtpTradeType

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1111111"


class AckIb(MockIb):
    """MockIb that acknowledges every placed order with ``Submitted``.

    The ack is delayed so that linked placement (which awaits the legs through ``gather``) has
    subscribed to each trade's status event before it fires.
    """

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        trade = super().placeOrder(contract, order)
        asyncio.get_running_loop().call_later(
            0.02, self.emit_status, trade.order.orderId, "Submitted"
        )
        return trade


async def _gateway() -> tuple[IbGatewayGateway, AckIb]:
    ib = AckIb()
    ib.managed_accounts = [ACCT]
    ib.client.next_req_id = 500
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = 0.5
    native = SimpleNamespace(symbol="AAPL", secType="STK", conId=1)
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=SimpleNamespace(native=native))
    resolver.resolve_from_pair = AsyncMock(return_value=SimpleNamespace(native=native))
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, ib


def _limit(side: TradeType = TradeType.BUY, price: str = "100") -> HBOrder:
    return HBOrder(order_type=OrderType.LIMIT, side=side, amount=Decimal(10), price=Decimal(price))


def _leg(role: LegRole, side: DtpTradeType, price: str) -> LegSpec:
    return LegSpec(
        role=role,
        order_type=DtpOrderType.LIMIT,
        trade_type=side,
        amount=Decimal(10),
        price=Decimal(price),
    )


def _stop_loss() -> LegSpec:
    return LegSpec(
        role=LegRole.STOP_LOSS,
        order_type=DtpOrderType.STOP_LOSS,
        trade_type=DtpTradeType.SELL,
        amount=Decimal(10),
        trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=Decimal(95)),
    )


def _bracket(*, stop: bool = True, take_profit: bool = True) -> LinkedOrderSpec:
    legs = [_leg(LegRole.ENTRY, DtpTradeType.BUY, "100")]
    if take_profit:
        legs.append(_leg(LegRole.TAKE_PROFIT, DtpTradeType.SELL, "110"))
    if stop:
        legs.append(_stop_loss())
    return LinkedOrderSpec(kind=LinkKind.BRACKET, legs=tuple(legs))


def _oco() -> LinkedOrderSpec:
    return LinkedOrderSpec(
        kind=LinkKind.OCO,
        legs=(
            _leg(LegRole.OCO_LEG, DtpTradeType.SELL, "110"),
            _leg(LegRole.OCO_LEG, DtpTradeType.SELL, "120"),
        ),
    )


def _refs(ib: AckIb) -> list[str]:
    return [o.orderRef for o in ib.placed_orders]


class TestSingleOrder:
    async def test_link_tag_becomes_order_ref(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_order(MagicMock(), _limit(), link_tag="mcl-5-0")  # type: ignore[call-arg]

        assert _refs(ib) == ["mcl-5-0"]

    async def test_distinct_tags_on_consecutive_orders(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_order(MagicMock(), _limit(), link_tag="mcl-7-0")  # type: ignore[call-arg]
        await gateway.place_order(MagicMock(), _limit(), link_tag="mcl-7-1")  # type: ignore[call-arg]

        assert _refs(ib) == ["mcl-7-0", "mcl-7-1"]

    async def test_no_link_tag_leaves_order_ref_empty(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_order(MagicMock(), _limit())

        assert [bool(ref) for ref in _refs(ib)] == [False]

    async def test_explicit_none_link_tag_leaves_order_ref_empty(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_order(MagicMock(), _limit(), link_tag=None)  # type: ignore[call-arg]

        assert [bool(ref) for ref in _refs(ib)] == [False]


class TestBracket:
    async def test_group_id_option_tags_each_leg_in_ordered_legs_order(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_bracket("AAPL", _bracket(), options={"group_id": 5})

        assert _refs(ib) == ["mcl-5-0", "mcl-5-1", "mcl-5-2"]

    async def test_single_exit_bracket_tags_entry_and_exit(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_bracket("AAPL", _bracket(stop=False), options={"group_id": 8})

        assert _refs(ib) == ["mcl-8-0", "mcl-8-1"]

    async def test_without_group_id_no_order_ref_is_set(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_bracket("AAPL", _bracket())

        assert [bool(ref) for ref in _refs(ib)] == [False, False, False]


class TestOco:
    async def test_group_id_option_tags_each_leg_in_ordered_legs_order(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_oco("AAPL", _oco(), options={"group_id": 6})

        assert _refs(ib) == ["mcl-6-0", "mcl-6-1"]

    async def test_without_group_id_no_order_ref_is_set(self) -> None:
        gateway, ib = await _gateway()

        await gateway.place_oco("AAPL", _oco())

        assert [bool(ref) for ref in _refs(ib)] == [False, False]
