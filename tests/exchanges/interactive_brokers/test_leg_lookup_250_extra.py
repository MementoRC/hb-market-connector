"""Extra #250 IB tests: ``list_open_legs`` spans clients; non-decoding link tags are accepted."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

pytest.importorskip("ib_async")


def _trade(order_id: int, client_id: int, ref: str) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol="AAPL", conId=1),
        order=SimpleNamespace(
            orderId=order_id, clientId=client_id, permId=0, orderRef=ref, account="DU1"
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0),
    )


def _gateway(trades: list[SimpleNamespace]) -> IbGatewayGateway:
    ib = MagicMock()
    ib.openTrades = MagicMock(return_value=trades)
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(client_id=5))
    transport._error_router._is_connected = True
    resolver = MagicMock()
    resolver.resolve_from_pair = AsyncMock(
        return_value=SimpleNamespace(native=SimpleNamespace(symbol="AAPL", conId=1))
    )
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)


async def test_list_open_legs_includes_tagged_orders_from_other_clients() -> None:
    gateway = _gateway([_trade(1, 5, "mcl-3-0"), _trade(2, 9, "mcl-3-1")])

    legs = await gateway.list_open_legs("AAPL", group="3")

    assert {leg.client_order_id for leg in legs} == {"1", "2"}
