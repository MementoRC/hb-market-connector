"""Coinbase OrderCapabilities (#238): exact declared set + capability contract.

Contract hook: ``validate_placement(gateway)`` returns ``async (OrderType) -> None`` that runs the
gateway's real ``place_order`` against a fake REST (no network); the contract only cares whether
it raises UnsupportedOrderError.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from data_type_primitives.common import OrderType

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.primitives import TradeType
from market_connector.protocols import HasCapabilities
from market_connector.testing.contract import (
    OrderCapabilitiesContractBase,
    check_order_capabilities,
)
from market_connector.transport.response import Response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_OK = {"success": True, "order_id": "o1", "success_response": {"order_id": "o1"}}


class _Rest:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, endpoint_name: str, **_: Any) -> Response:
        self.calls.append(endpoint_name)
        return Response(raw=_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


@pytest.fixture
def gateway() -> CoinbaseGateway:
    gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True))
    gw._rest = _Rest()  # type: ignore[assignment]
    gw._started = True
    return gw


class TestCoinbaseCapabilities:
    def test_is_has_capabilities(self, gateway: CoinbaseGateway) -> None:
        assert isinstance(gateway, HasCapabilities)

    def test_order_types_exactly_limit_limit_maker_market(self, gateway: CoinbaseGateway) -> None:
        caps = gateway.order_capabilities
        assert caps.order_types == frozenset(
            {OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET}
        )

    def test_no_triggers_trailing_or_linked(self, gateway: CoinbaseGateway) -> None:
        caps = gateway.order_capabilities
        assert caps.linked == frozenset()
        assert caps.native_linked is False
        assert caps.trigger_kinds == frozenset()
        assert caps.trigger_references == frozenset()
        assert caps.supports_trailing is False

    def test_descriptor_is_stable(self, gateway: CoinbaseGateway) -> None:
        assert gateway.order_capabilities == gateway.order_capabilities
        assert hash(gateway.order_capabilities) == hash(gateway.order_capabilities)


class TestCoinbaseCapabilityContract(OrderCapabilitiesContractBase):
    @pytest.fixture
    def gateway(self, gateway: CoinbaseGateway) -> CoinbaseGateway:
        return gateway

    def validate_placement(
        self, gateway: CoinbaseGateway
    ) -> Callable[[OrderType], Awaitable[None]]:
        async def validate(order_type: OrderType) -> None:
            await gateway.place_order(
                "BTC-USD",
                order_type,
                TradeType.BUY,
                Decimal("0.5"),
                Decimal("50000"),
                client_order_id="c1",
            )

        return validate


async def test_contract_fails_when_descriptor_overstates_coinbase(
    gateway: CoinbaseGateway,
) -> None:
    """Negative: advertising STOP_LOSS (unsupported by the adapter) is caught."""
    import dataclasses

    caps = gateway.order_capabilities
    inflated = dataclasses.replace(caps, order_types=caps.order_types | {OrderType.STOP_LOSS})

    async def validate(order_type: OrderType) -> None:
        await gateway.place_order(
            "BTC-USD", order_type, TradeType.BUY, Decimal("0.5"), Decimal("50000")
        )

    failures = await check_order_capabilities(inflated, validate)
    assert any("STOP_LOSS" in f for f in failures)
