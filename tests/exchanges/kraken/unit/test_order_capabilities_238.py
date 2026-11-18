"""Kraken OrderCapabilities (#238), derived from the adapter's placement code.

Source of truth: ``market_connector/exchanges/kraken/mixins/orders.py`` ``_ORDER_TYPE_MAP`` --
MARKET, LIMIT, LIMIT_MAKER (post-only), STOP_LOSS, TAKE_PROFIT, STOP_LOSS_LIMIT,
TAKE_PROFIT_LIMIT (#209/#212), TRAILING_STOP, TRAILING_STOP_LIMIT (#223). Kraken
brackets via native conditional close (P6 #240) and has no OCO, so ``linked`` is BRACKET only.

Contract hook: ``validate_placement(gateway)`` drives the real ``place_order`` with a fake REST.
Kraken currently raises ValueError for unknown types; the contract requires
UnsupportedOrderError (GREEN must convert ``_to_kraken_ordertype`` and update the two existing
ValueError assertions in test_canonical_order_type_231 / test_orders_mixin).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from data_type_primitives import TriggerKind, TriggerReference
from data_type_primitives.common import OrderType

from market_connector.capabilities import LinkedMode
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import TradeType
from market_connector.protocols import HasCapabilities
from market_connector.testing.contract import OrderCapabilitiesContractBase
from market_connector.transport.response import Response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OABC-XXXXX-YYYYYY"]}}
_EXPECTED_TYPES = frozenset(
    {
        OrderType.MARKET,
        OrderType.LIMIT,
        OrderType.LIMIT_MAKER,
        OrderType.STOP_LOSS,
        OrderType.TAKE_PROFIT,
        OrderType.STOP_LOSS_LIMIT,
        OrderType.TAKE_PROFIT_LIMIT,
        OrderType.TRAILING_STOP,
        OrderType.TRAILING_STOP_LIMIT,
    }
)
# order type -> (price, limit_price) valid for that type
_ARGS: dict[OrderType, tuple[Decimal | None, Decimal | None]] = {
    OrderType.MARKET: (None, None),
    OrderType.LIMIT: (Decimal("50000"), None),
    OrderType.LIMIT_MAKER: (Decimal("50000"), None),
    OrderType.STOP_LOSS: (Decimal("49000"), None),
    OrderType.TAKE_PROFIT: (Decimal("51000"), None),
    OrderType.STOP_LOSS_LIMIT: (Decimal("49000"), Decimal("48900")),
    OrderType.TAKE_PROFIT_LIMIT: (Decimal("51000"), Decimal("51100")),
    OrderType.TRAILING_STOP: (Decimal("150.5"), None),
    OrderType.TRAILING_STOP_LIMIT: (Decimal("150.5"), Decimal("-5")),
}


class _Rest:
    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        return Response(
            raw=_ADD_ORDER_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name
        )


@pytest.fixture
def gateway() -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = _Rest()  # type: ignore[assignment]
    gw._started = True
    return gw


class TestKrakenCapabilities:
    def test_is_has_capabilities(self, gateway: KrakenGateway) -> None:
        assert isinstance(gateway, HasCapabilities)

    def test_order_types_match_adapter_order_type_map(self, gateway: KrakenGateway) -> None:
        assert gateway.order_capabilities.order_types == _EXPECTED_TYPES

    def test_trailing_supported(self, gateway: KrakenGateway) -> None:
        assert gateway.order_capabilities.supports_trailing is True

    def test_trigger_vocabulary(self, gateway: KrakenGateway) -> None:
        caps = gateway.order_capabilities
        assert TriggerKind.ABSOLUTE in caps.trigger_kinds
        assert caps.trigger_references == frozenset({TriggerReference.LAST})

    def test_bracket_native_no_oco(self, gateway: KrakenGateway) -> None:
        assert gateway.order_capabilities.linked == frozenset({LinkedMode.BRACKET})
        assert gateway.order_capabilities.native_linked is True


class TestKrakenCapabilityContract(OrderCapabilitiesContractBase):
    @pytest.fixture
    def gateway(self, gateway: KrakenGateway) -> KrakenGateway:
        return gateway

    def validate_placement(self, gateway: KrakenGateway) -> Callable[[OrderType], Awaitable[None]]:
        async def validate(order_type: OrderType) -> None:
            price, limit_price = _ARGS.get(order_type, (Decimal("1"), None))
            await gateway.place_order(
                "XBTUSD",
                order_type,
                TradeType.BUY,
                Decimal("0.5"),
                price,
                limit_price=limit_price,
            )

        return validate
