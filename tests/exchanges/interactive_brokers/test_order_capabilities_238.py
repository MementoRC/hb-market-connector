"""Interactive Brokers OrderCapabilities (#238) + capability contract.

Contract hook: ``validate_placement(gateway)`` builds a valid ``HBOrder`` for the type (with a
trigger/price for the stop types, per #237) and runs ``_hb_to_ib_order`` -- the earliest
network-free point that raises UnsupportedOrderError. Non-advertised probes are limited to
``LIMIT_MAKER`` because ``HBOrder`` can only carry local-enum members (MARKET, LIMIT,
LIMIT_MAKER, STOP_LOSS, STOP_LOSS_LIMIT).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from data_type_primitives import TriggerKind, TriggerReference, TriggerSpec
from data_type_primitives.common import OrderType as DtpOrderType

from market_connector import capabilities
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.transport import _hb_to_ib_order
from market_connector.orders import HBOrder, TradeType
from market_connector.protocols import HasCapabilities
from market_connector.testing.contract import (
    OrderCapabilitiesContractBase,
    check_order_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytest.importorskip("ib_async")

_TRIGGER = TriggerSpec(
    kind=TriggerKind.ABSOLUTE, value=Decimal(95), reference=TriggerReference.LAST
)


def _order(order_type: DtpOrderType) -> HBOrder:
    kwargs: dict[str, object] = {}
    if order_type in (DtpOrderType.STOP_LOSS, DtpOrderType.STOP_LOSS_LIMIT):
        kwargs["trigger"] = _TRIGGER
    if order_type != DtpOrderType.MARKET:
        kwargs["price"] = Decimal(100)
    return HBOrder(
        order_type=order_type,
        side=TradeType.BUY,
        amount=Decimal(10),
        **kwargs,  # type: ignore[arg-type]
    )


def _validate(order_type: DtpOrderType) -> Awaitable[None]:
    async def run() -> None:
        _hb_to_ib_order(_order(order_type))

    return run()


@pytest.fixture
def gateway() -> IbGatewayGateway:
    return IbGatewayGateway(transport=MagicMock(), signer=MagicMock())


class TestIbCapabilities:
    def test_is_has_capabilities(self, gateway: IbGatewayGateway) -> None:
        assert isinstance(gateway, HasCapabilities)

    def test_order_types(self, gateway: IbGatewayGateway) -> None:
        assert gateway.order_capabilities.order_types == frozenset(
            {
                DtpOrderType.MARKET,
                DtpOrderType.LIMIT,
                DtpOrderType.STOP_LOSS,
                DtpOrderType.STOP_LOSS_LIMIT,
            }
        )

    def test_limit_maker_not_advertised(self, gateway: IbGatewayGateway) -> None:
        assert DtpOrderType.LIMIT_MAKER not in gateway.order_capabilities.order_types

    def test_triggers_and_trailing(self, gateway: IbGatewayGateway) -> None:
        caps = gateway.order_capabilities
        assert caps.trigger_kinds == frozenset({TriggerKind.ABSOLUTE})
        assert caps.trigger_references == frozenset({TriggerReference.LAST})
        assert caps.supports_trailing is False

    def test_native_bracket_and_oco(self, gateway: IbGatewayGateway) -> None:
        caps = gateway.order_capabilities
        assert caps.linked == frozenset(
            {capabilities.LinkedMode.BRACKET, capabilities.LinkedMode.OCO}
        )
        assert caps.native_linked is True


class TestIbCapabilityContract(OrderCapabilitiesContractBase):
    unsupported_probes = (DtpOrderType.LIMIT_MAKER,)

    @pytest.fixture
    def gateway(self, gateway: IbGatewayGateway) -> IbGatewayGateway:
        return gateway

    def validate_placement(
        self, gateway: IbGatewayGateway
    ) -> Callable[[DtpOrderType], Awaitable[None]]:
        return _validate


async def test_contract_fails_when_descriptor_adds_unsupported_type(
    gateway: IbGatewayGateway,
) -> None:
    """Negative: IB's descriptor plus LIMIT_MAKER (rejected by _hb_to_ib_order) is flagged."""
    caps = gateway.order_capabilities
    inflated = dataclasses.replace(caps, order_types=caps.order_types | {DtpOrderType.LIMIT_MAKER})
    failures = await check_order_capabilities(
        inflated, _validate, unsupported_probes=[DtpOrderType.LIMIT_MAKER]
    )
    assert any("LIMIT_MAKER" in f for f in failures)
