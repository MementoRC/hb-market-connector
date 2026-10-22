"""Stage 3 integration smoke test — exercises live IB Gateway market data path.

Skipped unless IB_GATEWAY_HOST is set (same gate as test_integration.py).
Host/port/client id come from IB_GATEWAY_HOST / IB_GATEWAY_PORT / IB_CLIENT_ID.
Requires market data subscriptions on the target gateway.
"""

from __future__ import annotations

import os

import pytest

if not os.environ.get("IB_GATEWAY_HOST"):
    pytest.skip(
        "Set IB_GATEWAY_HOST to run integration tests against a live IB Gateway",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_get_orderbook_returns_non_empty_snapshot_for_aapl() -> None:
    """Smoke: open gateway → resolve AAPL stock → fetch L2 depth → assert non-empty."""
    from market_connector.exchanges.interactive_brokers.exceptions import (
        MarketDataPermissionError,
    )
    from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

    depth_permission_codes = {2152, 10089, 10090, 354}

    spec = IbConnectionSpec(
        host=os.environ.get("IB_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("IB_GATEWAY_PORT", "4002")),
        client_id=int(os.environ.get("IB_CLIENT_ID", "999")),
        paper=True,  # resolved_market_data_type → 3 (delayed)
    )
    gateway = build_ib_gateway(spec)

    try:
        await gateway.start()
        try:
            snapshot = await gateway.get_orderbook("AAPL-USD")
        except MarketDataPermissionError as exc:
            if exc.error_code not in depth_permission_codes:
                raise
            pytest.skip(f"Account lacks L2 depth permissions: {exc}")
        assert snapshot.trading_pair == "AAPL-USD"
        assert len(snapshot.bids) > 0, "no bids returned — market closed or no data permissions"
        assert len(snapshot.asks) > 0, "no asks returned — market closed or no data permissions"
    finally:
        await gateway.stop()
