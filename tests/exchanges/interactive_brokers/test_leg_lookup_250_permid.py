"""RED acceptance tests for #250: completed-order lookup by recorded permId.

Live evidence (IB paper Gateway): ``reqCompletedOrders`` returns every completed trade with
``order.orderId == 0``, ``order.clientId == 0``, ``fills == []`` (even when Filled), usually
``orderRef == ""`` and the real ``permId``. A by-id match on ``orderId``/``clientId`` can never
hit a completed order.

Design pinned here: while an order is open the connector records
``str(orderId)`` (scoped to this clientId) -> ``permId`` from ``openOrderEvent`` /
``orderStatusEvent`` updates; ``get_leg(client_order_id=...)`` matches completed trades by that
recorded permId. The map is in-process only. Link-tag lookup is unchanged (covered by
``test_leg_lookup_250.py::TestGetLegByLinkTag``).

Events are driven through real ``ib_async.Event`` objects after ``transport._attach_handlers()``,
so the handlers must be subscribed there (the existing hook point for ``execDetailsEvent``).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

ib_async = pytest.importorskip("ib_async")

OWN = 5
SYMBOL = "AAPL"
PERM = 1234567
ORDER_ID = 42


def _open_trade(
    order_id: int = ORDER_ID, perm_id: int = PERM, *, client_id: int = OWN
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=SYMBOL, conId=1),
        order=SimpleNamespace(
            orderId=order_id, clientId=client_id, permId=perm_id, orderRef="", account="DU1"
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0),
    )


def _completed_trade(perm_id: int, status: str, filled: float = 0.0) -> SimpleNamespace:
    """Live shape: orderId=0, clientId=0, no fills, empty orderRef, real permId."""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=SYMBOL, conId=1),
        order=SimpleNamespace(orderId=0, clientId=0, permId=perm_id, orderRef="", account="DU1"),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0),
        fills=[],
    )


def _build(completed: list[Any]) -> tuple[IbGatewayGateway, MagicMock]:
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    ib.openTrades = MagicMock(return_value=[])
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=list(completed))
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    ib.openOrderEvent = ib_async.Event("openOrderEvent")
    ib.orderStatusEvent = ib_async.Event("orderStatusEvent")
    with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(client_id=OWN))
    transport._error_router._is_connected = True
    transport._attach_handlers()
    resolver = MagicMock()
    resolver.resolve_from_pair = AsyncMock(
        return_value=SimpleNamespace(native=SimpleNamespace(symbol=SYMBOL, conId=1))
    )
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, ib


_TERMINAL = [
    ("Cancelled", 0.0, OrderState.CANCELED),
    ("Filled", 1.0, OrderState.FILLED),
]


@pytest.mark.parametrize(("status", "filled", "expected"), _TERMINAL)
async def test_open_order_seen_via_open_order_event_is_found_in_completed(
    status: str, filled: float, expected: OrderState
) -> None:
    gateway, ib = _build([_completed_trade(PERM, status, filled)])
    ib.openOrderEvent.emit(_open_trade())

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert (snap.state, snap.filled, snap.venue_order_id) == (
        expected,
        Decimal(str(filled)),
        str(PERM),
    )
    assert snap.client_order_id == str(ORDER_ID)


@pytest.mark.parametrize(("status", "filled", "expected"), _TERMINAL)
async def test_perm_id_recorded_from_order_status_event_alone(
    status: str, filled: float, expected: OrderState
) -> None:
    gateway, ib = _build([_completed_trade(PERM, status, filled)])
    ib.orderStatusEvent.emit(_open_trade())

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert (snap.state, snap.client_order_id) == (expected, str(ORDER_ID))


async def test_completed_trade_with_unrecorded_perm_id_is_not_matched() -> None:
    gateway, ib = _build([_completed_trade(999, "Filled", 1.0)])
    ib.openOrderEvent.emit(_open_trade())  # records PERM, not 999

    assert await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID)) is None


async def test_client_order_id_zero_never_matches_completed_zero_ids() -> None:
    gateway, _ = _build([_completed_trade(PERM, "Filled", 1.0)])

    assert await gateway.get_leg(SYMBOL, client_order_id="0") is None


async def test_other_clients_open_order_perm_id_is_not_recorded() -> None:
    gateway, ib = _build([_completed_trade(PERM, "Cancelled")])
    ib.openOrderEvent.emit(_open_trade(client_id=9))

    assert await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID)) is None


async def test_fresh_connector_without_recorded_map_returns_none() -> None:
    """In-process limit: after a restart a closed order is not found by client_order_id."""
    gateway, _ = _build([_completed_trade(PERM, "Filled", 1.0)])

    assert await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID)) is None
