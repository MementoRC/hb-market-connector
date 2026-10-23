"""Acceptance tests for issue #58a: time-in-force and outside-RTH on orders."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, OrderType, TradeType

pytest.importorskip("ib_async")

TIF_VALUES = ["DAY", "GTC", "IOC"]


def _time_in_force() -> Any:
    # Imported lazily so guard tests still run while TimeInForce is missing (red phase).
    from market_connector.orders import TimeInForce  # noqa: PLC0415

    return TimeInForce


def _make(order_type: OrderType, **extra: Any) -> HBOrder:
    price = Decimal("100.5") if order_type == OrderType.LIMIT else None
    return HBOrder(
        order_type=order_type,
        side=TradeType.BUY,
        amount=Decimal("10"),
        price=price,
        **extra,
    )


ORDER_TYPES = [OrderType.MARKET, OrderType.LIMIT]


class TestHBOrderFields:
    def test_defaults(self):
        order = _make(OrderType.MARKET)
        assert order.time_in_force is _time_in_force().DAY
        assert order.outside_rth is False

    def test_frozen_time_in_force(self):
        order = _make(OrderType.MARKET)
        with pytest.raises(ValidationError):
            order.time_in_force = _time_in_force().GTC

    def test_accepts_enum(self):
        order = _make(OrderType.MARKET, time_in_force=_time_in_force().IOC)
        assert order.time_in_force is _time_in_force().IOC

    def test_accepts_string_value(self):
        order = _make(OrderType.MARKET, time_in_force="GTC")
        assert order.time_in_force is _time_in_force().GTC

    def test_invalid_value_raises(self):
        with pytest.raises(ValidationError):
            _make(OrderType.MARKET, time_in_force="FOK")


class TestMappingTif:
    @pytest.mark.parametrize("tif", TIF_VALUES)
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    def test_tif_mapped(self, order_type: OrderType, tif: str):
        ib_order = _hb_to_ib_order(_make(order_type, time_in_force=tif))
        assert ib_order.tif == tif


class TestMappingOutsideRth:
    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    def test_outside_rth_true(self, order_type: OrderType):
        ib_order = _hb_to_ib_order(_make(order_type, outside_rth=True))
        assert ib_order.outsideRth is True

    @pytest.mark.parametrize("order_type", ORDER_TYPES)
    def test_defaults_unchanged(self, order_type: OrderType):
        ib_order = _hb_to_ib_order(_make(order_type))
        assert ib_order.tif == "DAY"
        assert ib_order.outsideRth is False


class TestGuards:
    def test_limit_mapping_unchanged(self):
        ib_order = _hb_to_ib_order(_make(OrderType.LIMIT))
        assert ib_order.orderType == "LMT"
        assert ib_order.action == "BUY"
        assert ib_order.totalQuantity == 10.0
        assert ib_order.lmtPrice == 100.5

    def test_market_mapping_unchanged(self):
        ib_order = _hb_to_ib_order(_make(OrderType.MARKET))
        assert ib_order.orderType == "MKT"
        assert ib_order.action == "BUY"
        assert ib_order.totalQuantity == 10.0

    def test_limit_maker_still_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _hb_to_ib_order(_make(OrderType.LIMIT_MAKER))
