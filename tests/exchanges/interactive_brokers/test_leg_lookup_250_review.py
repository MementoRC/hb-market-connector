"""PR #309 review follow-ups for IB leg lookup (#250): account, contract and refresh scoping."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

pytest.importorskip("ib_async")

SYMBOL = "AAPL"


def _trade(
    order_id: int,
    *,
    ref: str = "mcl-5-0",
    symbol: str = SYMBOL,
    con_id: int = 1,
    account: str = "DU1",
    client_id: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, conId=con_id),
        order=SimpleNamespace(
            orderId=order_id, clientId=client_id, permId=0, orderRef=ref, account=account
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0),
    )


def _build(
    open_trades: list[Any],
    *,
    account: str | None = None,
    spec: IbConnectionSpec | None = None,
    target_con_id: int = 1,
) -> tuple[IbGatewayGateway, MagicMock]:
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    ib.openTrades = MagicMock(return_value=list(open_trades))
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
        transport = IbGatewayTransport(spec or IbConnectionSpec(client_id=5))
    transport._error_router._is_connected = True
    transport._account = account
    resolver = MagicMock()

    async def _from_pair(pair: str) -> Any:
        return SimpleNamespace(native=SimpleNamespace(symbol=pair, conId=target_con_id))

    resolver.resolve_from_pair = AsyncMock(side_effect=_from_pair)
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver), ib


class TestAccountScope:
    async def test_get_leg_skips_other_accounts(self) -> None:
        gateway, _ = _build([_trade(1, account="DU2"), _trade(2, account="DU1")], account="DU1")

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is None
        snap = await gateway.get_leg(SYMBOL, client_order_id="2")
        assert snap is not None
        assert snap.client_order_id == "2"

    async def test_get_leg_by_link_tag_skips_other_accounts(self) -> None:
        gateway, _ = _build([_trade(1, account="DU2")], account="DU1")

        assert await gateway.get_leg(SYMBOL, link_tag="mcl-5-0") is None

    async def test_list_open_legs_skips_other_accounts(self) -> None:
        gateway, _ = _build(
            [_trade(1, account="DU2", ref="mcl-5-0"), _trade(2, account="DU1", ref="mcl-5-1")],
            account="DU1",
        )

        legs = await gateway.list_open_legs(SYMBOL)

        assert {leg.client_order_id for leg in legs} == {"2"}

    async def test_no_configured_account_keeps_all(self) -> None:
        gateway, _ = _build([_trade(1, account="DU2")], account=None)

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is not None


class TestContractScope:
    async def test_get_leg_rejects_other_instrument_by_symbol(self) -> None:
        gateway, _ = _build([_trade(1, symbol="MSFT", con_id=0)])

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is None

    async def test_get_leg_rejects_same_symbol_different_con_id(self) -> None:
        gateway, _ = _build([_trade(1, con_id=99)], target_con_id=1)

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is None
        assert await gateway.get_leg(SYMBOL, link_tag="mcl-5-0") is None

    async def test_get_leg_matches_same_contract(self) -> None:
        gateway, _ = _build([_trade(1, con_id=7)], target_con_id=7)

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is not None

    async def test_list_open_legs_rejects_same_symbol_different_con_id(self) -> None:
        gateway, _ = _build(
            [_trade(1, con_id=99), _trade(2, ref="mcl-5-1", con_id=7)], target_con_id=7
        )

        legs = await gateway.list_open_legs(SYMBOL)

        assert {leg.client_order_id for leg in legs} == {"2"}


class TestOpenOrderRefresh:
    async def test_stale_other_client_order_not_listed_after_refresh_drops_it(self) -> None:
        stale = _trade(9, client_id=9, ref="mcl-5-0")
        gateway, ib = _build([stale])

        async def _refresh() -> list[Any]:
            ib.openTrades.return_value = []
            return []

        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=_refresh)

        assert await gateway.list_open_legs(SYMBOL) == []
        ib.reqAllOpenOrdersAsync.assert_awaited_once()

    async def test_link_tag_lookup_refreshes_before_reading_cache(self) -> None:
        gateway, ib = _build([_trade(9, client_id=9)])

        async def _refresh() -> list[Any]:
            ib.openTrades.return_value = []
            return []

        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=_refresh)

        assert await gateway.get_leg(SYMBOL, link_tag="mcl-5-0") is None
        ib.reqAllOpenOrdersAsync.assert_awaited_once()

    async def test_by_id_lookup_does_not_refresh(self) -> None:
        gateway, ib = _build([_trade(1)])

        assert await gateway.get_leg(SYMBOL, client_order_id="1") is not None
        ib.reqAllOpenOrdersAsync.assert_not_called()

    @pytest.mark.parametrize(
        "spec",
        [
            IbConnectionSpec(client_id=5, fetch_all_open_orders=False),
            IbConnectionSpec(client_id=5, readonly=True),
        ],
    )
    async def test_no_refresh_when_disabled_or_readonly(self, spec: IbConnectionSpec) -> None:
        gateway, ib = _build([_trade(1)], spec=spec)

        assert len(await gateway.list_open_legs(SYMBOL)) == 1
        ib.reqAllOpenOrdersAsync.assert_not_called()

    async def test_failed_refresh_falls_back_to_cache(self) -> None:
        gateway, ib = _build([_trade(1)])
        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=RuntimeError("boom"))

        assert len(await gateway.list_open_legs(SYMBOL)) == 1
