"""Acceptance: a gateway built by build_ib_gateway() resolves contracts in every mixin call (#68).

The mixins read the resolver the factory injected. Nothing here sets
``_contract_resolver`` / ``contract_resolver`` by hand.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from ._doubles import MockIb
from .test_persistent_market_data import _feed_levels

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.gateway import IbGatewayGateway

_PAIR = "AAPL-USD"
_CONTRACT = MagicMock(conId=265598, symbol="AAPL")  # hashable, like a real Contract


@pytest.fixture
def mock_ib() -> MockIb:
    ib = MockIb()
    ib._l1_ticker.bid = 100.0  # valid top of book, pre-set for get_mid_price
    ib._l1_ticker.ask = 100.5
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
        return build_ib_gateway(IbConnectionSpec(market_data_timeout=0.2))


async def _call_get_orderbook(g: IbGatewayGateway) -> object:
    # The depth stream only exists once the call starts: feed raw ticks shortly after.
    ticker = g._transport._ib._depth_ticker
    asyncio.get_running_loop().call_later(
        0.01, lambda: _feed_levels(ticker, [(100.0, 5.0)], [(100.5, 7.0)])
    )
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
