"""Integration test against a real IB Gateway.

Skipped by default at module level UNLESS the IB_GATEWAY_HOST environment
variable is set. This makes the gate independent of pytest -m filtering --
the tests literally cannot run unless explicitly enabled.

To run:
    1. Start IB Gateway via Docker: gnzsnz/ib-gateway with paper credentials.
       See README for the recommended docker-compose setup.
    2. Set env vars: IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4002 [IB_PAPER_ACCOUNT=...]
    3. Run: pixi run test tests/exchanges/interactive_brokers/test_integration.py

Requires:
    - IBKR_PAPER_USER and IBKR_PAPER_PASSWORD env vars (consumed by the Docker container).
    - IB Gateway listening on the configured port.
"""

from __future__ import annotations

import os

import pytest

# Module-level skip if env var not set. allow_module_level=True ensures the
# import-time check stops collection entirely (no test functions registered).
if not os.environ.get("IB_GATEWAY_HOST"):
    pytest.skip(
        "Set IB_GATEWAY_HOST to run integration tests against a live IB Gateway",
        allow_module_level=True,
    )

from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

pytestmark = pytest.mark.ib_gateway


@pytest.fixture
def paper_spec() -> IbConnectionSpec:
    return IbConnectionSpec(
        host=os.environ.get("IB_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("IB_GATEWAY_PORT", "4002")),
        client_id=int(os.environ.get("IB_CLIENT_ID", "999")),  # avoid clash with default
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )


@pytest.mark.asyncio
async def test_paper_gateway_round_trip(paper_spec: IbConnectionSpec) -> None:
    """Connect to a real paper IB Gateway, verify ready, disconnect."""
    g = build_ib_gateway(paper_spec)
    assert not g.ready

    try:
        await g.start()
        assert g.ready, "Expected IB Gateway to report connected after start()"
    finally:
        await g.stop()

    assert not g.ready
