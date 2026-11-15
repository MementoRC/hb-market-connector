"""IB raises typed errors (#236): UnsupportedOrderError, GatewayNotStartedError; ValueError kept."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector import exceptions as exc_mod
from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, OrderType, TradeType

pytest.importorskip("ib_async")


class TestUnsupportedOrderTypes:
    def test_limit_maker_raises_unsupported_order_error(self) -> None:
        order = HBOrder(
            order_type=OrderType.LIMIT_MAKER,
            side=TradeType.BUY,
            amount=Decimal(10),
            price=Decimal("100.5"),
        )
        with pytest.raises(exc_mod.UnsupportedOrderError) as exc:
            _hb_to_ib_order(order)
        assert not isinstance(exc.value, NotImplementedError)
        assert exc.value.exchange == "interactive_brokers"

    def test_unknown_order_type_raises_unsupported_order_error(self) -> None:
        order = SimpleNamespace(
            order_type="WEIRD", side=TradeType.BUY, amount=Decimal(1), price=None, stop_price=None
        )
        with pytest.raises(exc_mod.UnsupportedOrderError) as exc:
            _hb_to_ib_order(order)  # type: ignore[arg-type]
        assert exc.value.exchange == "interactive_brokers"


class TestMalformedInputStaysValueError:
    def test_limit_without_price(self) -> None:
        order = HBOrder(order_type=OrderType.LIMIT, side=TradeType.BUY, amount=Decimal(1))
        with pytest.raises(ValueError, match="price"):
            _hb_to_ib_order(order)

    def test_market_with_stop_price(self) -> None:
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="trigger"):
            HBOrder(
                order_type=OrderType.MARKET,
                side=TradeType.BUY,
                amount=Decimal(1),
                stop_price=Decimal(5),
            )

    def test_stop_loss_without_trigger(self) -> None:
        with pytest.raises(ValueError, match="trigger"):
            HBOrder(order_type=OrderType.STOP_LOSS, side=TradeType.BUY, amount=Decimal(1))


class TestGatewayWithoutResolver:
    @staticmethod
    def _gateway() -> tuple[IbGatewayGateway, MagicMock]:
        transport = MagicMock()
        transport.place_bracket = AsyncMock()
        transport.place_oca = AsyncMock()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=None)
        return gateway, transport

    async def test_place_bracket_raises_gateway_not_started(self) -> None:
        gateway, transport = self._gateway()
        with pytest.raises(GatewayNotStartedError):
            await gateway.place_bracket(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        transport.place_bracket.assert_not_called()

    async def test_place_oca_raises_gateway_not_started(self) -> None:
        gateway, transport = self._gateway()
        with pytest.raises(GatewayNotStartedError):
            await gateway.place_oca(MagicMock(), [MagicMock(), MagicMock()])
        transport.place_oca.assert_not_called()
