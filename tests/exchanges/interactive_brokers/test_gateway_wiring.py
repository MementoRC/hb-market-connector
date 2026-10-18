"""Acceptance: a gateway built by build_ib_gateway() resolves contracts in every mixin call (#68).

The mixins read the resolver the factory injected. Nothing here sets
``_contract_resolver`` / ``contract_resolver`` by hand.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from ._doubles import MockIb

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.gateway import IbGatewayGateway

_PAIR = "AAPL-USD"
_CONTRACT = SimpleNamespace(conId=265598, symbol="AAPL")


def _level(price: float, size: float) -> SimpleNamespace:
    return SimpleNamespace(price=price, size=size)


@pytest.fixture
def mock_ib() -> MockIb:
    ib = MockIb()
    ib._depth_ticker.domBids = [_level(100.0, 5)]
    ib._depth_ticker.domAsks = [_level(100.5, 7)]
    ib._historical_bars = [
        SimpleNamespace(date="20260102 15:30:00", open=1.0, high=2.0, low=0.5, close=1.5, volume=10)
    ]
    return ib


@pytest.fixture
def gateway(mock_ib: MockIb) -> IbGatewayGateway:
    with patch(
        "market_connector.exchanges.interactive_brokers.transport.IB",
        return_value=mock_ib,
    ):
        built = build_ib_gateway(IbConnectionSpec())
    # Market data now comes from the transport (#54); this test is about resolver wiring only.
    built._transport.top_of_book = AsyncMock(return_value=(Decimal("100.0"), Decimal("100.5")))  # type: ignore[method-assign]
    built._transport.depth_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=([(Decimal("100.0"), Decimal("5"))], [(Decimal("100.5"), Decimal("7"))])
    )
    return built


async def _call_get_orderbook(g: IbGatewayGateway) -> object:
    return await g.get_orderbook(_PAIR)


async def _call_get_mid_price(g: IbGatewayGateway) -> object:
    return await g.get_mid_price(_PAIR)


async def _call_get_candles(g: IbGatewayGateway) -> object:
    return await g.get_candles(_PAIR, "1m", 1)


async def _call_subscribe_orderbook(g: IbGatewayGateway) -> None:
    async with g.subscribe_orderbook(_PAIR, lambda _u: None):
        pass


async def _call_subscribe_trades(g: IbGatewayGateway) -> None:
    async with g.subscribe_trades(_PAIR, lambda _t: None):
        pass


@pytest.mark.parametrize(
    "call",
    [
        _call_get_orderbook,
        _call_get_mid_price,
        _call_get_candles,
        _call_subscribe_orderbook,
        _call_subscribe_trades,
    ],
)
async def test_factory_built_gateway_resolves_through_factory_resolver(
    gateway: IbGatewayGateway,
    call,
) -> None:
    with patch.object(
        gateway.contract_resolver,
        "resolve_from_pair",
        new=AsyncMock(return_value=_CONTRACT),
    ) as resolve:
        await call(gateway)

    resolve.assert_awaited_once_with(_PAIR)
