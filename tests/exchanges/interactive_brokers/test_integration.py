"""Integration test against a real IB Gateway.

Skipped by default at module level UNLESS the IB_GATEWAY_HOST environment
variable is set. This makes the gate independent of pytest -m filtering --
the tests literally cannot run unless explicitly enabled.

To run:
    1. Start IB Gateway via Docker: gnzsnz/ib-gateway with paper credentials.
       See README for the recommended docker-compose setup.
    2. Set env vars: IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4002 [IB_PAPER_ACCOUNT=...]
       (IB_GATEWAY_PORT defaults to 4002; use e.g. 4012 if 4002 is taken locally.
       Live ports 4001/7496 are refused; a live Gateway on a non-default port
       cannot be detected, so always point at a paper Gateway.)
    3. Run: IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4012 \\
           pixi run -e default pytest -m ib_gateway --no-cov \\
           tests/exchanges/interactive_brokers/test_integration.py

Requires:
    - IBKR_PAPER_USER and IBKR_PAPER_PASSWORD env vars (consumed by the Docker container).
    - IB Gateway listening on the configured port.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

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

from ._live import live_endpoint

if TYPE_CHECKING:
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.ib_gateway


@pytest.fixture
def paper_spec() -> IbConnectionSpec:
    host, port = live_endpoint()  # fails on live-trading ports before anything connects
    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=int(os.environ.get("IB_CLIENT_ID", "999")),  # avoid clash with default
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )
    assert spec.paper
    return spec


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


_FLATTEN_TIMEOUT_S = 30.0
_SETTLE_TIMEOUT_S = 5.0


async def _cancel_and_flatten(gateway: Any, ref: InstrumentRef, handle: OrderHandle) -> None:
    """Cancel a still-working order, then offset any filled quantity with a MARKET SELL.

    Waits (bounded) until the offsetting order is FILLED so the paper account ends flat.
    """
    from decimal import Decimal

    from market_connector.exchanges.interactive_brokers.order_handle import (
        IBOrderState,
        OrderHandle,
    )
    from market_connector.orders import HBOrder, OrderType, TradeType

    terminal = {IBOrderState.FILLED, IBOrderState.CANCELLED, IBOrderState.REJECTED}
    current = OrderHandle.from_trade(handle._trade)
    if current.status not in terminal:
        try:
            await gateway.cancel_order(current)
        except Exception:
            # cancel can race a fill (e.g. IB 161); still flatten whatever filled.
            logger.exception("cancel_order failed during cleanup; continuing to flatten")
        deadline = asyncio.get_running_loop().time() + _SETTLE_TIMEOUT_S
        current = OrderHandle.from_trade(handle._trade)
        while current.status not in terminal and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            current = OrderHandle.from_trade(handle._trade)

    if current.filled_qty <= 0:
        return

    offset = await gateway.place_order(
        ref,
        HBOrder(
            order_type=OrderType.MARKET,
            side=TradeType.SELL,
            amount=Decimal(current.filled_qty),
            price=None,
        ),
    )
    if offset.status is not IBOrderState.FILLED:
        offset = await offset.wait_for(status=IBOrderState.FILLED, timeout=_FLATTEN_TIMEOUT_S)


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
    from market_connector.exchanges.interactive_brokers.order_handle import IBOrderState
    from market_connector.orders import HBOrder, OrderType, TradeType

    spec = paper_spec
    gateway = build_ib_gateway(spec)
    placed: OrderHandle | None = None
    ref: InstrumentRef | None = None
    body_ok = False

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
        placed = handle
        assert handle.status in {IBOrderState.SUBMITTED, IBOrderState.FILLED}, (
            f"Unexpected status after place_order: {handle.status!r}"
        )

        # 3. Cancel (idempotent if already FILLED).
        cancelled = await gateway.cancel_order(handle)
        assert cancelled.status in {IBOrderState.CANCELLED, IBOrderState.FILLED}, (
            f"Unexpected status after cancel_order: {cancelled.status!r}"
        )
        body_ok = True
    finally:
        try:
            if placed is not None and ref is not None:
                await _cancel_and_flatten(gateway, ref, placed)
        except Exception as cleanup_exc:
            if body_ok:
                raise
            # Never mask the original test failure with a cleanup error.
            msg = (
                f"Paper-account cleanup failed ({cleanup_exc!r}); "
                "account may not be flat; check positions"
            )
            logger.exception(msg)
        finally:
            await gateway.stop()
