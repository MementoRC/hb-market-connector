"""Acceptance tests for issue #237: IB STP / STP LMT built from ``trigger``.

An order built from ``trigger=TriggerSpec(ABSOLUTE, X)`` must map to an IB order
identical to the one built from the deprecated ``stop_price=X`` (wire behaviour
unchanged).
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TriggerKind, TriggerSpec

from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, OrderType, TradeType

pytest.importorskip("ib_async")

STOP = Decimal("95.25")
LIMIT_PRICE = Decimal("94.75")

_CASES = [
    pytest.param(OrderType.STOP_LOSS, None, "STP", id="stp"),
    pytest.param(OrderType.STOP_LOSS_LIMIT, LIMIT_PRICE, "STP LMT", id="stp-lmt"),
]


def _from_trigger(order_type: OrderType, price: Decimal | None, **extra: Any) -> HBOrder:
    return HBOrder(
        order_type=order_type,
        side=TradeType.SELL,
        amount=Decimal("10"),
        price=price,
        trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=STOP),
        **extra,
    )


def _from_stop_price(order_type: OrderType, price: Decimal | None, **extra: Any) -> HBOrder:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return HBOrder(
            order_type=order_type,
            side=TradeType.SELL,
            amount=Decimal("10"),
            price=price,
            stop_price=STOP,
            **extra,
        )


@pytest.mark.parametrize(("order_type", "price", "ib_type"), _CASES)
class TestTriggerMapsLikeStopPrice:
    def test_aux_and_lmt_price_identical(
        self, order_type: OrderType, price: Decimal | None, ib_type: str
    ):
        via_trigger = _hb_to_ib_order(_from_trigger(order_type, price))
        via_stop_price = _hb_to_ib_order(_from_stop_price(order_type, price))
        assert via_trigger.orderType == ib_type
        assert via_trigger.auxPrice == via_stop_price.auxPrice == float(STOP)
        assert via_trigger.lmtPrice == via_stop_price.lmtPrice

    def test_whole_order_identical(
        self, order_type: OrderType, price: Decimal | None, ib_type: str
    ):
        via_trigger = _hb_to_ib_order(_from_trigger(order_type, price))
        via_stop_price = _hb_to_ib_order(_from_stop_price(order_type, price))
        # ib_async.Order.__eq__ is identity, so compare field by field.
        assert vars(via_trigger) == vars(via_stop_price)

    def test_tif_and_outside_rth_identical(
        self, order_type: OrderType, price: Decimal | None, ib_type: str
    ):
        extra: dict[str, Any] = {"time_in_force": "GTC", "outside_rth": True}
        via_trigger = _hb_to_ib_order(_from_trigger(order_type, price, **extra))
        via_stop_price = _hb_to_ib_order(_from_stop_price(order_type, price, **extra))
        assert via_trigger.tif == via_stop_price.tif == "GTC"
        assert via_trigger.outsideRth is via_stop_price.outsideRth is True
        assert vars(via_trigger) == vars(via_stop_price)

    def test_mapping_from_trigger_emits_no_deprecation_warning(
        self, order_type: OrderType, price: Decimal | None, ib_type: str
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            _hb_to_ib_order(_from_trigger(order_type, price))


class TestStpSpecifics:
    def test_stp_from_trigger_leaves_lmt_price_unset(self):
        from ib_async import Order  # noqa: PLC0415

        ib_order = _hb_to_ib_order(_from_trigger(OrderType.STOP_LOSS, None))
        assert ib_order.lmtPrice == Order().lmtPrice

    def test_stp_lmt_from_trigger_sets_both_prices(self):
        ib_order = _hb_to_ib_order(_from_trigger(OrderType.STOP_LOSS_LIMIT, LIMIT_PRICE))
        assert ib_order.auxPrice == float(STOP)
        assert ib_order.lmtPrice == float(LIMIT_PRICE)


class TestMismatchRejectedAtConstruction:
    """IB-side trigger/type ValueErrors moved into the HBOrder validators."""

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    def test_trigger_on_market_or_limit_fails_before_transport(self, order_type: OrderType):
        price = Decimal("100") if order_type == OrderType.LIMIT else None
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            _from_trigger(order_type, price)

    @pytest.mark.parametrize("order_type", [OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT])
    def test_conditional_without_trigger_fails_before_transport(self, order_type: OrderType):
        price = LIMIT_PRICE if order_type == OrderType.STOP_LOSS_LIMIT else None
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            HBOrder(order_type=order_type, side=TradeType.SELL, amount=Decimal("10"), price=price)

    def test_stop_limit_without_price_fails_before_transport(self):
        with pytest.raises(ValueError, match=r"(?i)price"):
            _from_trigger(OrderType.STOP_LOSS_LIMIT, None)
