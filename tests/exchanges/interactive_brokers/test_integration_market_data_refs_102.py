"""Live market data via InstrumentRef (issue #102). READ-ONLY: no orders.

Skipped by default at module level UNLESS IB_GATEWAY_HOST is set (same gate as
test_integration_asset_classes_57.py).

To run (paper Gateway only; live ports 4001/7496 are refused):
    IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4002 [IB_CLIENT_ID=958] [IB_PAPER_ACCOUNT=...] \\
        pixi run -e default pytest -m ib_gateway --no-cov \\
        tests/exchanges/interactive_brokers/test_integration_market_data_refs_102.py

The test is skipped (not failed) when the account lacks a crypto market-data subscription.
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
from market_connector.exchanges.interactive_brokers.exceptions import MarketDataPermissionError
from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from ._live import live_endpoint, live_readonly

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
        readonly=live_readonly(),
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
async def test_crypto_btc_mid_price_by_instrument_ref_or_skip_without_permission() -> None:
    ref = InstrumentRef(
        symbol="BTC",
        instrument_type=InstrumentType.CRYPTO,
        quote_currency="USD",
        exchange_hint="PAXOS",
    )
    async with _connected() as gateway:
        try:
            mid = await gateway.get_mid_price(ref)  # type: ignore[arg-type]
        except MarketDataPermissionError as exc:
            pytest.skip(f"no crypto market-data permission (IB code {exc.error_code})")
        assert isinstance(mid, Decimal)
        assert mid > 0
