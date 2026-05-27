"""Stage 3 integration smoke test — exercises live IB Gateway market data path.

Skipped unless IB_LIVE=1 is set in environment. Requires a running IB Gateway
or TWS on localhost:4002 (paper trading port) with market data subscriptions.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("IB_LIVE") != "1",
        reason="set IB_LIVE=1 to run live IB Gateway integration tests",
    ),
]


@pytest.mark.asyncio
async def test_get_orderbook_returns_non_empty_snapshot_for_aapl() -> None:
    """Smoke: open gateway → resolve AAPL stock → fetch L2 depth → assert non-empty."""
    from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

    spec = IbConnectionSpec(paper=True)  # resolved_market_data_type → 3 (delayed)
    gateway = build_ib_gateway(spec)

    try:
        await gateway.start()
        snapshot = await gateway.get_orderbook("AAPL-USD")
        assert snapshot.trading_pair == "AAPL-USD"
        assert len(snapshot.bids) > 0, "no bids returned — market closed or no data permissions"
        assert len(snapshot.asks) > 0, "no asks returned — market closed or no data permissions"
    finally:
        await gateway.stop()
