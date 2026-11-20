"""Acceptance tests for issue #241: remove the deprecated ``HBOrder.stop_price`` alias.

Contract (issue #241, #210 P7):
- ``HBOrder(..., stop_price=X)`` is rejected with a pydantic unknown/extra-field error.
- ``HBOrder`` has no ``stop_price`` field or attribute, and no ``_map_stop_price`` shim.
- The ``trigger=`` path is unchanged.
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TriggerKind, TriggerSpec
from pydantic import ValidationError

from market_connector.orders import HBOrder, OrderType, TradeType

STOP = Decimal("95.25")
LIMIT_PRICE = Decimal("94.75")
AMOUNT = Decimal("10")


def _order(order_type: OrderType, **fields: Any) -> HBOrder:
    return HBOrder(order_type=order_type, side=TradeType.SELL, amount=AMOUNT, **fields)


def _absolute() -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.ABSOLUTE, value=STOP)


class TestStopPriceRejected:
    @pytest.mark.parametrize("order_type", [OrderType.STOP_LOSS, OrderType.MARKET])
    def test_stop_price_kwarg_is_rejected(self, order_type: OrderType):
        with pytest.raises(ValidationError, match="stop_price"):
            _order(order_type, stop_price=STOP)

    def test_stop_price_with_trigger_is_rejected_as_extra_field(self):
        with pytest.raises(ValidationError, match="stop_price"):
            _order(OrderType.STOP_LOSS, trigger=_absolute(), stop_price=STOP)

    def test_rejection_is_extra_forbidden_error(self):
        with pytest.raises(ValidationError) as excinfo:
            _order(OrderType.STOP_LOSS, stop_price=STOP)
        assert any(err["type"] == "extra_forbidden" for err in excinfo.value.errors())

    def test_stop_price_is_not_a_model_field(self):
        assert "stop_price" not in HBOrder.model_fields

    def test_instance_has_no_stop_price_attribute(self):
        order = _order(OrderType.STOP_LOSS, trigger=_absolute())
        assert not hasattr(order, "stop_price")

    def test_deprecation_shim_is_gone(self):
        assert not hasattr(HBOrder, "_map_stop_price")


class TestTriggerPathUnchanged:
    def test_stop_loss_with_trigger_builds_without_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            order = _order(OrderType.STOP_LOSS, trigger=_absolute())
        assert order.trigger == _absolute()

    def test_stop_loss_limit_with_trigger_and_price_builds(self):
        order = _order(OrderType.STOP_LOSS_LIMIT, trigger=_absolute(), price=LIMIT_PRICE)
        assert order.trigger == _absolute()
        assert order.price == LIMIT_PRICE

    def test_conditional_type_without_trigger_is_rejected(self):
        with pytest.raises(ValueError, match="trigger"):
            _order(OrderType.STOP_LOSS)

    def test_order_stays_frozen_and_hashable(self):
        order = _order(OrderType.STOP_LOSS, trigger=_absolute())
        assert hash(order) == hash(_order(OrderType.STOP_LOSS, trigger=_absolute()))
