"""Local OrderType/TradeType stay name-aligned with data_type_primitives.common (#231)."""

from __future__ import annotations

import pytest
from data_type_primitives.common import OrderType as DtpOrderType
from data_type_primitives.common import TradeType as DtpTradeType

from market_connector.hb_compat.primitives import (
    OrderType,
    TradeType,
    from_canonical,
    to_canonical,
)


def test_every_local_order_type_name_exists_in_dtp() -> None:
    assert {m.name for m in OrderType} <= set(DtpOrderType.__members__)


def test_every_local_trade_type_name_exists_in_dtp() -> None:
    assert {m.name for m in TradeType} <= set(DtpTradeType.__members__)


@pytest.mark.parametrize("member", list(OrderType), ids=lambda m: m.name)
def test_order_type_round_trips_by_name(member: OrderType) -> None:
    canonical = to_canonical(member)
    assert isinstance(canonical, DtpOrderType)
    assert canonical.name == member.name
    assert from_canonical(canonical) is member


@pytest.mark.parametrize("member", list(TradeType), ids=lambda m: m.name)
def test_trade_type_round_trips_by_name(member: TradeType) -> None:
    canonical = to_canonical(member)
    assert isinstance(canonical, DtpTradeType)
    assert canonical.name == member.name
    assert from_canonical(canonical) is member


@pytest.mark.parametrize("member", list(OrderType), ids=lambda m: m.name)
def test_local_order_type_value_is_its_name(member: OrderType) -> None:
    assert member.value == member.name
