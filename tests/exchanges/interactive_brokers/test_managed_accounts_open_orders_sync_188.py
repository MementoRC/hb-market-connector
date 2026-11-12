"""Acceptance tests for issue #188: managed_accounts() and sync_open_orders().

Red phase: new names are resolved inside test bodies (exception classes via the module
attribute), so each test fails on its own and collection never fails.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU111"  # must be one of the managed accounts served by MockIb
OWN_CLIENT_ID = 5
OTHER_CLIENT_ID = 9


def _exc() -> Any:
    from market_connector.exchanges.interactive_brokers import exceptions as ib_exc  # noqa: PLC0415

    return ib_exc


def _make(account_id: str | None = ACCT) -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = ["DU111", "DU222"]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(
            IbConnectionSpec(account_id=account_id, client_id=OWN_CLIENT_ID)
        )
    return transport, ib


def _make_readonly() -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = ["DU111", "DU222"]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(
            IbConnectionSpec(account_id=ACCT, client_id=OWN_CLIENT_ID, readonly=True)
        )
    return transport, ib


def _trade(order_id: int, client_id: int) -> Any:
    """Real ib_async Trade, as openTrades() serves them."""
    from ib_async import Contract, Order, OrderStatus, Trade  # noqa: PLC0415

    return Trade(
        contract=Contract(symbol="AAPL"),
        order=Order(orderId=order_id, clientId=client_id),
        orderStatus=OrderStatus(orderId=order_id, status="Submitted"),
    )


def _cache_filled_by_req_all(ib: MockIb, trades: list[Any]) -> AsyncMock:
    """reqAllOpenOrdersAsync that, like ib_async, populates the openTrades() cache."""
    cache: list[Any] = []

    async def fake_req_all() -> None:
        cache[:] = trades

    ib.openTrades = lambda: list(cache)  # type: ignore[method-assign]
    return AsyncMock(side_effect=fake_req_all)


class TestManagedAccounts:
    async def test_returns_managed_accounts_list(self) -> None:
        transport, _ib = _make()
        await transport.connect()
        assert transport.managed_accounts() == ["DU111", "DU222"]

    async def test_returned_list_is_a_copy(self) -> None:
        transport, ib = _make()
        await transport.connect()
        result = transport.managed_accounts()
        result.append("DU999")
        assert ib.managed_accounts == ["DU111", "DU222"]

    async def test_empty_list_raises_unavailable(self) -> None:
        transport, ib = _make()
        ib.managed_accounts = []
        await transport.connect()
        ib_exc = _exc()
        with pytest.raises(ib_exc.ManagedAccountsUnavailableError):
            transport.managed_accounts()
        assert issubclass(ib_exc.ManagedAccountsUnavailableError, ib_exc.IbError)

    async def test_not_connected_raises_connection_lost(self) -> None:
        transport, _ib = _make()
        ib_exc = _exc()
        with pytest.raises(ib_exc.ConnectionLostError):
            transport.managed_accounts()


class TestManagedAccountsGateway:
    def test_gateway_delegates(self) -> None:
        transport = MagicMock()
        transport.managed_accounts = MagicMock(return_value=["DU1"])
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert gateway.managed_accounts() == ["DU1"]
        transport.managed_accounts.assert_called_once_with()


class TestSyncOpenOrders:
    async def test_awaits_req_all_once_and_exposes_other_and_manual_orders(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.reqAllOpenOrdersAsync = _cache_filled_by_req_all(
            ib, [_trade(7, OTHER_CLIENT_ID), _trade(-1, 0)]
        )
        await transport.sync_open_orders(timeout=2.0)
        ib.reqAllOpenOrdersAsync.assert_awaited_once_with()

        handles = {h.order_id: h for h in transport.open_orders()}
        other = handles[7]
        assert other.client_id == OTHER_CLIENT_ID
        assert other.owned is False
        manual = handles[-1]
        assert manual.client_id == 0
        assert manual.manual is True
        assert manual.owned is False


class TestSyncOpenOrdersErrors:
    async def test_timeout_raises_timeout_error(self) -> None:
        transport, ib = _make()
        await transport.connect()

        async def hang() -> None:
            await asyncio.sleep(10)

        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=hang)
        ib_exc = _exc()
        with pytest.raises(ib_exc.OpenOrdersSyncTimeoutError):
            await transport.sync_open_orders(timeout=0.05)
        assert issubclass(ib_exc.OpenOrdersSyncTimeoutError, ib_exc.OpenOrdersSyncError)
        assert issubclass(ib_exc.OpenOrdersSyncError, ib_exc.IbError)

    async def test_unexpected_error_wrapped_with_cause(self) -> None:
        transport, ib = _make()
        await transport.connect()
        boom = RuntimeError("boom")
        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=boom)
        ib_exc = _exc()
        with pytest.raises(ib_exc.OpenOrdersSyncError) as info:
            await transport.sync_open_orders(timeout=2.0)
        assert info.value.__cause__ is boom

    async def test_ib_error_propagates_unchanged(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib_exc = _exc()
        err = ib_exc.IbReadOnlyApiError("READ_ONLY_API enabled")
        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=err)
        with pytest.raises(ib_exc.IbReadOnlyApiError) as info:
            await transport.sync_open_orders(timeout=2.0)
        assert info.value is err


class TestSyncOpenOrdersGuards:
    async def test_not_connected_raises_and_does_not_call_ib(self) -> None:
        transport, ib = _make()
        ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
        ib_exc = _exc()
        with pytest.raises(ib_exc.ConnectionLostError):
            await transport.sync_open_orders(timeout=2.0)
        ib.reqAllOpenOrdersAsync.assert_not_awaited()

    async def test_readonly_raises_and_does_not_call_ib(self) -> None:
        transport, ib = _make_readonly()
        await transport.connect()
        ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
        ib_exc = _exc()
        with pytest.raises(ib_exc.IbReadOnlyModeError):
            await transport.sync_open_orders(timeout=2.0)
        ib.reqAllOpenOrdersAsync.assert_not_awaited()

    @pytest.mark.parametrize("bad", [0, -1])
    async def test_non_positive_timeout_raises_value_error(self, bad: float) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
        with pytest.raises(ValueError):
            await transport.sync_open_orders(timeout=bad)
        ib.reqAllOpenOrdersAsync.assert_not_awaited()


class TestSyncOpenOrdersGateway:
    async def test_gateway_delegates_timeout(self) -> None:
        transport = MagicMock()
        transport.sync_open_orders = AsyncMock(return_value=None)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        await gateway.sync_open_orders(timeout=3.0)
        transport.sync_open_orders.assert_awaited_once()
        call = transport.sync_open_orders.await_args
        assert call.args == (3.0,) or call.kwargs == {"timeout": 3.0}
