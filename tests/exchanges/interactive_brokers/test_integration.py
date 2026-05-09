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


@pytest.mark.asyncio
async def test_paper_aapl_resolve_place_cancel(paper_spec: IbConnectionSpec) -> None:
    """Resolve AAPL, place a paper MARKET BUY 1 share, await SUBMITTED, cancel.

    Verifies the full Stage 2 path end-to-end:
      - IbContractResolver.resolve() returns a non-empty contract_id
      - transport.place_order() returns SUBMITTED or FILLED
      - transport.cancel_order() returns CANCELLED (or FILLED if it filled first)

    Requires a running IB Gateway in paper mode. Set IB_GATEWAY_HOST to enable.
    Skipped automatically in CI (IB_GATEWAY_HOST is never set in CI).
    """
    from decimal import Decimal

    from market_connector.contracts.instrument import InstrumentRef, InstrumentType
    from market_connector.exchanges.interactive_brokers.order_handle import OrderState
    from market_connector.orders import HBOrder, OrderType, TradeType

    spec = paper_spec
    gateway = build_ib_gateway(spec)

    try:
        await gateway.start()

        # 1. Resolve AAPL — must return a valid contract_id (non-empty conId string).
        ref = InstrumentRef(
            symbol="AAPL",
            instrument_type=InstrumentType.STOCK,
            quote_currency="USD",
        )
        resolved = await gateway.contract_resolver.resolve(ref)
        assert resolved.contract_id, "Expected non-empty contract_id for AAPL"

        # 2. Place a paper MARKET BUY 1 share.
        hb_order = HBOrder(
            order_type=OrderType.MARKET,
            side=TradeType.BUY,
            amount=Decimal("1"),
            price=None,
        )
        handle = await gateway.place_order(ref, hb_order)
        assert handle.status in {OrderState.SUBMITTED, OrderState.FILLED}, (
            f"Unexpected status after place_order: {handle.status!r}"
        )

        # 3. Cancel (idempotent if already FILLED).
        cancelled = await gateway.cancel_order(handle)
        assert cancelled.status in {OrderState.CANCELLED, OrderState.FILLED}, (
            f"Unexpected status after cancel_order: {cancelled.status!r}"
        )
    finally:
        await gateway.stop()
