"""Live paper-Gateway check of cross-client open-order visibility (#55).

Purpose: verify against a real IB Gateway that IB reports the originating
``clientId`` for open orders, so that ``OrderHandle.client_id`` / ``owned`` are
correct when another client (or a restarted bot) looks at the order, and that
non-owned orders are never cancelled (``cancel_all`` skips them,
``cancel_order`` raises ``ValueError``). ``reqGlobalCancel`` is never used.

Prerequisites:
    - Paper IB Gateway on IB_GATEWAY_HOST:IB_GATEWAY_PORT (port default 4002).
      IB_GATEWAY_HOST must be set (e.g. 127.0.0.1) to enable the module, as in
      test_integration.py. Live ports 4001/7496 are refused; a live Gateway on
      a non-default port cannot be detected, so point IB_GATEWAY_PORT at a
      paper Gateway.
    - US market hours (an order rejected by IB makes the test skip, not fail).
    - ib_async installed. Client ids 11..14 must be free.

The tests skip cleanly when no Gateway is reachable.

Run:
    IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4012 pixi run test -m ib_gateway \\
        tests/exchanges/interactive_brokers/test_live_client_id_visibility.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
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
from market_connector.exchanges.interactive_brokers.exceptions import OrderRejectedError
from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.order_handle import IBOrderState
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.orders import HBOrder, OrderType, TradeType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle

from ._live import live_endpoint

pytestmark = pytest.mark.ib_gateway

CLIENT_A, CLIENT_B, CLIENT_C, CLIENT_D = 11, 12, 13, 14
_WORKING = {"Submitted", "PreSubmitted"}
_AAPL = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK, quote_currency="USD")


def _spec(client_id: int, *, fetch_all: bool = True) -> IbConnectionSpec:
    host, port = live_endpoint()
    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=client_id,
        paper=True,
        fetch_all_open_orders=fetch_all,
    )
    assert spec.paper
    return spec


@pytest.fixture(autouse=True)
def _require_gateway() -> None:
    host, port = live_endpoint()  # fails on live-trading ports before any connection
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError as exc:
        pytest.skip(f"No IB paper Gateway reachable at {host}:{port} ({exc})")


@asynccontextmanager
async def _connected(client_id: int, *, fetch_all: bool = True) -> AsyncIterator[IbGatewayGateway]:
    gateway = build_ib_gateway(_spec(client_id, fetch_all=fetch_all))
    try:
        try:
            await gateway.start()
        except (OSError, TimeoutError) as exc:
            pytest.skip(f"IB paper Gateway API handshake failed ({client_id=}): {exc!r}")
        yield gateway
    finally:
        await gateway.stop()


async def _await_working(gateway: IbGatewayGateway, placed: OrderHandle) -> OrderHandle:
    """Poll until IB has assigned a permId and the order is working; return the fresh handle."""
    order_id = placed._trade.order.orderId
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for handle in gateway.get_open_orders():
            order = handle._trade.order
            if order.orderId == order_id and order.permId and handle.raw_status in _WORKING:
                return handle
        await asyncio.sleep(0.25)
    pytest.fail(f"order {order_id} never reached a working state")


def _find(gateway: IbGatewayGateway, perm_id: int) -> OrderHandle | None:
    # permId is the only order identity shared across clients in IB; orderId is per-client.
    return next((h for h in gateway.get_open_orders() if h._trade.order.permId == perm_id), None)


@pytest.fixture
async def working_order() -> AsyncIterator[tuple[IbGatewayGateway, OrderHandle]]:
    """Client A places an unfillable BUY LIMIT 1 AAPL @ 1.00 (DAY); always cancelled at teardown."""
    async with _connected(CLIENT_A) as gateway_a:
        order = HBOrder(
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("1.00"),
        )
        try:
            placed = await gateway_a.place_order(_AAPL, order)
        except OrderRejectedError as exc:
            pytest.skip(f"IB rejected the order (outside market hours?): {exc}")
        fresh: OrderHandle | None = None
        try:
            if placed.status is IBOrderState.REJECTED:
                pytest.skip(f"IB rejected the order: {placed.raw_status}")
            fresh = await _await_working(gateway_a, placed)
            yield gateway_a, fresh
        finally:
            await gateway_a.cancel_order(fresh or placed)


async def test_other_client_sees_order_as_not_owned(
    working_order: tuple[IbGatewayGateway, OrderHandle],
) -> None:
    gateway_a, handle_a = working_order
    perm_id = handle_a._trade.order.permId
    async with _connected(CLIENT_B) as gateway_b:
        seen = _find(gateway_b, perm_id)
        assert seen is not None, "client B does not see client A's order"
        assert seen.client_id == CLIENT_A
        assert seen.owned is False

        assert await gateway_b.cancel_all() == []
        with pytest.raises(ValueError):
            await gateway_b.cancel_order(seen)

    still = _find(gateway_a, perm_id)
    assert still is not None
    assert still.raw_status in _WORKING
    assert still.owned is True


async def test_fetch_all_disabled_hides_foreign_order(
    working_order: tuple[IbGatewayGateway, OrderHandle],
) -> None:
    _, handle_a = working_order
    async with _connected(CLIENT_C, fetch_all=False) as gateway_c:
        assert _find(gateway_c, handle_a._trade.order.permId) is None


async def test_restarted_bot_sees_order_but_cannot_cancel_it(
    working_order: tuple[IbGatewayGateway, OrderHandle],
) -> None:
    _, handle_a = working_order
    async with _connected(CLIENT_D) as restarted:
        seen = _find(restarted, handle_a._trade.order.permId)
        assert seen is not None, "restarted bot does not see the pre-existing order"
        assert seen.client_id == CLIENT_A
        assert seen.owned is False
        with pytest.raises(ValueError):
            await restarted.cancel_order(seen)
