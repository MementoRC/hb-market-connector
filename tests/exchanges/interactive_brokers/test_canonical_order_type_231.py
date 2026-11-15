"""IB takes a caller-supplied order type via ``HBOrder`` (#231).

``HBOrder.order_type`` / ``side`` must accept a ``data_type_primitives.common`` member or a
case-insensitive name string, normalised by member NAME to the local StrEnum the transport
compares against.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives.common import OrderType as DtpOrderType
from data_type_primitives.common import TradeType as DtpTradeType

from market_connector.orders import HBOrder, OrderType, TradeType

_IB_TYPES = ["MARKET", "LIMIT", "LIMIT_MAKER", "STOP_LOSS", "STOP_LOSS_LIMIT"]


def _order(order_type: Any, side: Any = TradeType.BUY) -> HBOrder:
    return HBOrder(order_type=order_type, side=side, amount=Decimal(1), price=Decimal(10))


@pytest.mark.parametrize("name", _IB_TYPES)
def test_local_enum_still_accepted(name: str) -> None:
    assert _order(OrderType[name]).order_type is OrderType[name]


@pytest.mark.parametrize("name", _IB_TYPES)
def test_dtp_member_normalised_to_local(name: str) -> None:
    assert _order(DtpOrderType[name]).order_type is OrderType[name]


@pytest.mark.parametrize("name", _IB_TYPES)
@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_string_normalised_by_name(name: str, case: Any) -> None:
    assert _order(case(name)).order_type is OrderType[name]


@pytest.mark.parametrize("side", [DtpTradeType.SELL, "sell", "SELL", TradeType.SELL])
def test_side_normalised(side: Any) -> None:
    assert _order(OrderType.LIMIT, side).side is TradeType.SELL
