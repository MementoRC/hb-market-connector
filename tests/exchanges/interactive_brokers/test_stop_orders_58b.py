"""Acceptance tests for issue #58b: stop orders and explicit LIMIT_MAKER rejection."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, OrderType, TradeType

pytest.importorskip("ib_async")

STOP = Decimal("95.25")
LIMIT_PRICE = Decimal("94.75")


def _ot(name: str) -> OrderType:
    # Looked up lazily so collection succeeds while the members are missing (red phase).
    return OrderType[name]


def _stop_loss(side: TradeType = TradeType.BUY, **extra: Any) -> HBOrder:
    fields: dict[str, Any] = {"stop_price": STOP, **extra}
    return HBOrder(order_type=_ot("STOP_LOSS"), side=side, amount=Decimal("10"), **fields)


def _stop_limit(**extra: Any) -> HBOrder:
    fields: dict[str, Any] = {"stop_price": STOP, "price": LIMIT_PRICE, **extra}
    return HBOrder(
        order_type=_ot("STOP_LOSS_LIMIT"), side=TradeType.SELL, amount=Decimal("10"), **fields
    )


class TestEnumMembers:
    @pytest.mark.parametrize("name", ["STOP_LOSS", "STOP_LOSS_LIMIT"])
    def test_local_primitives_has_member(self, name: str):
        from market_connector import primitives  # noqa: PLC0415

        assert primitives.OrderType[name].value == name

    @pytest.mark.parametrize("name", ["STOP_LOSS", "STOP_LOSS_LIMIT"])
    def test_orders_has_member(self, name: str):
        assert OrderType[name].value == name

    @pytest.mark.parametrize("name", ["STOP_LOSS", "STOP_LOSS_LIMIT"])
    def test_matches_canonical(self, name: str):
        common = pytest.importorskip("data_type_primitives.common")
        canonical = common.OrderType[name]
        assert canonical.name == OrderType[name].name


class TestHBOrderField:
    def test_stop_price_defaults_none(self):
        order = HBOrder(order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal("1"))
        assert order.stop_price is None


class TestStopLossMapping:
    @pytest.mark.parametrize("side", [TradeType.BUY, TradeType.SELL])
    def test_stp_mapping(self, side: TradeType):
        ib_order = _hb_to_ib_order(_stop_loss(side))
        assert ib_order.orderType == "STP"
        assert ib_order.auxPrice == float(STOP)
        assert ib_order.action == side.value
        assert ib_order.totalQuantity == 10.0
        assert ib_order.tif == "DAY"
        assert ib_order.outsideRth is False

    def test_stp_does_not_set_lmt_price(self):
        ib_order = _hb_to_ib_order(_stop_loss())
        assert ib_order.lmtPrice in (0.0, None) or ib_order.lmtPrice != ib_order.lmtPrice

    def test_stp_gtc_outside_rth(self):
        ib_order = _hb_to_ib_order(_stop_loss(time_in_force="GTC", outside_rth=True))
        assert ib_order.orderType == "STP"
        assert ib_order.tif == "GTC"
        assert ib_order.outsideRth is True


class TestStopLossLimitMapping:
    def test_stp_lmt_mapping(self):
        ib_order = _hb_to_ib_order(_stop_limit())
        assert ib_order.orderType == "STP LMT"
        assert ib_order.auxPrice == float(STOP)
        assert ib_order.lmtPrice == float(LIMIT_PRICE)
        assert ib_order.action == "SELL"
        assert ib_order.totalQuantity == 10.0
        assert ib_order.tif == "DAY"

    def test_stp_lmt_gtc_outside_rth(self):
        ib_order = _hb_to_ib_order(_stop_limit(time_in_force="GTC", outside_rth=True))
        assert ib_order.tif == "GTC"
        assert ib_order.outsideRth is True


class TestValidation:
    def test_stop_loss_requires_stop_price(self):
        order = HBOrder(order_type=_ot("STOP_LOSS"), side=TradeType.BUY, amount=Decimal("10"))
        with pytest.raises(ValueError, match="stop_price"):
            _hb_to_ib_order(order)

    def test_stop_limit_requires_stop_price(self):
        order = HBOrder(
            order_type=_ot("STOP_LOSS_LIMIT"),
            side=TradeType.BUY,
            amount=Decimal("10"),
            price=LIMIT_PRICE,
        )
        with pytest.raises(ValueError, match="stop_price"):
            _hb_to_ib_order(order)

    def test_stop_limit_requires_price(self):
        order = HBOrder(
            order_type=_ot("STOP_LOSS_LIMIT"),
            side=TradeType.BUY,
            amount=Decimal("10"),
            stop_price=STOP,
        )
        with pytest.raises(ValueError, match="price"):
            _hb_to_ib_order(order)

    def test_market_with_stop_price_rejected(self):
        order = HBOrder(
            order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal("10"), stop_price=STOP
        )
        with pytest.raises(ValueError, match="stop_price"):
            _hb_to_ib_order(order)

    def test_limit_with_stop_price_rejected(self):
        order = HBOrder(
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.5"),
            stop_price=STOP,
        )
        with pytest.raises(ValueError, match="stop_price"):
            _hb_to_ib_order(order)


class TestLimitMakerRejected:
    def test_limit_maker_message(self):
        order = HBOrder(
            order_type=OrderType.LIMIT_MAKER,
            side=TradeType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.5"),
        )
        with pytest.raises(NotImplementedError, match=r"(?i)post-only") as exc:
            _hb_to_ib_order(order)
        assert "limit" in str(exc.value).lower()
        assert "LIMIT" in str(exc.value)


class TestGuards:
    def test_limit_mapping_unchanged(self):
        order = HBOrder(
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("10"),
            price=Decimal("100.5"),
        )
        ib_order = _hb_to_ib_order(order)
        assert ib_order.orderType == "LMT"
        assert ib_order.lmtPrice == 100.5
        assert ib_order.action == "BUY"
        assert ib_order.tif == "DAY"

    def test_market_mapping_unchanged(self):
        order = HBOrder(order_type=OrderType.MARKET, side=TradeType.SELL, amount=Decimal("10"))
        ib_order = _hb_to_ib_order(order)
        assert ib_order.orderType == "MKT"
        assert ib_order.action == "SELL"
        assert ib_order.totalQuantity == 10.0

    def test_tif_58a_unchanged(self):
        order = HBOrder(
            order_type=OrderType.MARKET,
            side=TradeType.BUY,
            amount=Decimal("10"),
            time_in_force="IOC",
            outside_rth=True,
        )
        ib_order = _hb_to_ib_order(order)
        assert ib_order.tif == "IOC"
        assert ib_order.outsideRth is True
