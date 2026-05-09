"""Tests for _hb_to_ib_order order-type mapper (Stage 2: MARKET + LIMIT)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, OrderType, TradeType


def _make_order(
    order_type: OrderType,
    side: TradeType,
    amount: Decimal = Decimal("10"),
    price: Decimal | None = None,
) -> HBOrder:
    return HBOrder(order_type=order_type, side=side, amount=amount, price=price)


class TestMarketOrders:
    def test_market_buy_maps_to_mkt_buy(self):
        mock_order_cls = MagicMock()
        with patch.dict("sys.modules", {"ib_async": MagicMock(Order=mock_order_cls)}):
            _hb_to_ib_order(_make_order(OrderType.MARKET, TradeType.BUY))
        mock_order_cls.assert_called_once_with(
            action="BUY",
            orderType="MKT",
            totalQuantity=10.0,
        )

    def test_market_sell_maps_to_mkt_sell(self):
        mock_order_cls = MagicMock()
        with patch.dict("sys.modules", {"ib_async": MagicMock(Order=mock_order_cls)}):
            _hb_to_ib_order(_make_order(OrderType.MARKET, TradeType.SELL))
        mock_order_cls.assert_called_once_with(
            action="SELL",
            orderType="MKT",
            totalQuantity=10.0,
        )


class TestLimitOrders:
    def test_limit_buy_with_price(self):
        mock_order_cls = MagicMock()
        with patch.dict("sys.modules", {"ib_async": MagicMock(Order=mock_order_cls)}):
            _hb_to_ib_order(_make_order(OrderType.LIMIT, TradeType.BUY, price=Decimal("123.45")))
        mock_order_cls.assert_called_once_with(
            action="BUY",
            orderType="LMT",
            totalQuantity=10.0,
            lmtPrice=123.45,
        )

    def test_limit_sell_with_price(self):
        mock_order_cls = MagicMock()
        with patch.dict("sys.modules", {"ib_async": MagicMock(Order=mock_order_cls)}):
            _hb_to_ib_order(_make_order(OrderType.LIMIT, TradeType.SELL, price=Decimal("99.99")))
        mock_order_cls.assert_called_once_with(
            action="SELL",
            orderType="LMT",
            totalQuantity=10.0,
            lmtPrice=99.99,
        )

    def test_limit_without_price_raises_value_error(self):
        with (
            patch.dict("sys.modules", {"ib_async": MagicMock()}),
            pytest.raises(ValueError, match="price"),
        ):
            _hb_to_ib_order(_make_order(OrderType.LIMIT, TradeType.BUY, price=None))


class TestUnsupportedOrderTypes:
    @pytest.mark.parametrize(
        "order_type",
        [
            OrderType.LIMIT_MAKER,
        ],
    )
    def test_unsupported_types_raise_not_implemented(self, order_type: OrderType):
        with (
            patch.dict("sys.modules", {"ib_async": MagicMock()}),
            pytest.raises(NotImplementedError, match="Stage 2"),
        ):
            _hb_to_ib_order(_make_order(order_type, TradeType.BUY))
