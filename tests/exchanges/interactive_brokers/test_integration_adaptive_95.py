"""Live Adaptive-algo what-if check (issue #95). READ-ONLY: what_if only, no order is placed.

Skipped by default at module level UNLESS IB_GATEWAY_HOST is set (same gate as
test_integration_asset_classes_57.py).

To run (paper Gateway only; live ports 4001/7496 are refused):
    IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4002 [IB_CLIENT_ID=958] [IB_PAPER_ACCOUNT=...] \\
        pixi run -e default pytest -m ib_gateway --no-cov \\
        tests/exchanges/interactive_brokers/test_integration_adaptive_95.py
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if not os.environ.get("IB_GATEWAY_HOST"):
    pytest.skip(
        "Set IB_GATEWAY_HOST to run integration tests against a live IB Gateway",
        allow_module_level=True,
    )

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.order_options import (
    AdaptivePriority,
    IbOrderOptions,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.what_if import IbWhatIfResult
from market_connector.orders import HBOrder, OrderType, TradeType

from ._live import live_endpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway

pytestmark = pytest.mark.ib_gateway


def _paper_spec() -> IbConnectionSpec:
    host, port = live_endpoint()  # fails on live-trading ports before anything connects
    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=int(os.environ.get("IB_CLIENT_ID", "958")),
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )
    assert spec.paper
    return spec


@asynccontextmanager
async def _connected() -> AsyncIterator[IbGatewayGateway]:
    gateway = build_ib_gateway(_paper_spec())
    try:
        await gateway.start()
        yield gateway
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_what_if_aapl_limit_with_adaptive_normal_returns_result() -> None:
    ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK, quote_currency="USD")
    order = HBOrder(
        order_type=OrderType.LIMIT,
        side=TradeType.BUY,
        amount=Decimal(1),
        price=Decimal("1.00"),
    )
    options = IbOrderOptions(adaptive_priority=AdaptivePriority.NORMAL)
    async with _connected() as gateway:
        result = await gateway.what_if(ref, order, options=options)
        assert isinstance(result, IbWhatIfResult)
