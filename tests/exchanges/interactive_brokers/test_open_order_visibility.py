"""Acceptance tests for issue #55: open-order visibility across client ids.

AC1..AC7 are named in each test class docstring. Written goal-first (red) before
the implementation exists.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

OWN_CLIENT_ID = 5
OTHER_CLIENT_ID = 9


@pytest.fixture(autouse=True)
def _patch_ib_async_module():
    """ib_async is an optional extra; inject a MagicMock so lazy imports succeed."""
    with patch.dict(sys.modules, {"ib_async": MagicMock()}):
        yield


def _trade(order_id: int, client_id: int, status: str = "Submitted") -> MagicMock:
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = 0
    trade.order.clientId = client_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = 0.0
    trade.orderStatus.avgFillPrice = 0.0

    handlers: list = []

    def iadd(_self, cb):
        handlers.append(cb)
        return trade.statusEvent

    def isub(_self, cb):
        if cb in handlers:
            handlers.remove(cb)
        return trade.statusEvent

    def call(*args):
        for h in list(handlers):
            h(*args)

    trade.statusEvent.__iadd__ = iadd
    trade.statusEvent.__isub__ = isub
    trade.statusEvent.side_effect = call
    return trade


def _mock_ib() -> MagicMock:
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.reqExecutionsAsync = AsyncMock(return_value=[])
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.isConnected = MagicMock(return_value=True)
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    return ib


def _transport(ib: MagicMock, **spec_kwargs: object) -> IbGatewayTransport:
    spec_kwargs.setdefault("client_id", OWN_CLIENT_ID)
    with patch(
        "market_connector.exchanges.interactive_brokers.transport.IB",
        return_value=ib,
    ):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kwargs))
    transport._error_router._is_connected = True
    return transport


class TestAC1SpecFields:
    """AC1: fetch_all_open_orders flag and client_id validation."""

    def test_fetch_all_open_orders_defaults_true(self) -> None:
        assert IbConnectionSpec().fetch_all_open_orders is True

    def test_fetch_all_open_orders_can_be_disabled(self) -> None:
        assert IbConnectionSpec(fetch_all_open_orders=False).fetch_all_open_orders is False

    def test_client_id_zero_is_valid(self) -> None:
        assert IbConnectionSpec(client_id=0).client_id == 0

    @pytest.mark.parametrize("bad", [-1, -100])
    def test_negative_client_id_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="client_id"):
            IbConnectionSpec(client_id=bad)

    @pytest.mark.parametrize("bad", [True, 1.0, "1"])
    def test_non_int_client_id_rejected(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            IbConnectionSpec(client_id=bad)  # type: ignore[arg-type]


class TestAC2HandleOwnership:
    """AC2: OrderHandle exposes client_id and owned; open_orders() computes owned."""

    def test_from_trade_exposes_client_id(self) -> None:
        handle = OrderHandle.from_trade(_trade(1, OTHER_CLIENT_ID))
        assert handle.client_id == OTHER_CLIENT_ID

    def test_open_orders_owned_matches_spec_client_id(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [
            _trade(1, OWN_CLIENT_ID),
            _trade(2, OTHER_CLIENT_ID),
            _trade(3, 0),
        ]
        handles = _transport(ib).open_orders()
        assert [(h.client_id, h.owned) for h in handles] == [
            (OWN_CLIENT_ID, True),
            (OTHER_CLIENT_ID, False),
            (0, False),
        ]


class TestAC3FetchAllOnConnect:
    """AC3: reqAllOpenOrdersAsync once per successful connect, gated by the flag."""

    async def test_called_once_on_connect(self) -> None:
        ib = _mock_ib()
        await _transport(ib)._connect_once()
        ib.reqAllOpenOrdersAsync.assert_awaited_once_with()

    async def test_called_again_on_reconnect(self) -> None:
        ib = _mock_ib()
        transport = _transport(ib)
        await transport._connect_once()
        await transport._connect_once()
        assert ib.reqAllOpenOrdersAsync.await_count == 2

    async def test_not_called_when_disabled(self) -> None:
        ib = _mock_ib()
        await _transport(ib, fetch_all_open_orders=False)._connect_once()
        ib.reqAllOpenOrdersAsync.assert_not_called()

    async def test_failure_warns_and_connect_succeeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ib = _mock_ib()
        ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=RuntimeError("boom"))
        transport = _transport(ib)
        with caplog.at_level(logging.WARNING):
            await transport._connect_once()
        assert transport._error_router.is_connected is True
        assert any(
            r.levelno == logging.WARNING and "boom" in r.getMessage() for r in caplog.records
        )


class TestAC4CrossClientVisibility:
    """AC4: open_orders() reports other clients' orders with correct flags."""

    def test_other_client_order_visible_not_owned(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [_trade(7, OTHER_CLIENT_ID)]
        (handle,) = _transport(ib).open_orders()
        assert handle.order_id == 7
        assert handle.client_id == OTHER_CLIENT_ID
        assert handle.owned is False

    def test_own_order_owned(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [_trade(8, OWN_CLIENT_ID)]
        (handle,) = _transport(ib).open_orders()
        assert handle.client_id == OWN_CLIENT_ID
        assert handle.owned is True


class TestAC5CancelNonOwned:
    """AC5: cancelling a non-owned handle is refused (ValueError); owned still works."""

    async def test_non_owned_cancel_raises_and_does_not_call_ib(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [_trade(7, OTHER_CLIENT_ID)]
        transport = _transport(ib)
        (handle,) = transport.open_orders()
        with pytest.raises(ValueError, match="(?i)client"):
            await transport.cancel_order(handle)
        ib.cancelOrder.assert_not_called()

    async def test_owned_cancel_still_calls_ib(self) -> None:
        ib = _mock_ib()
        trade = _trade(8, OWN_CLIENT_ID)
        ib.openTrades.return_value = [trade]
        transport = _transport(ib)
        (handle,) = transport.open_orders()

        def _cancel(_order: object) -> None:
            trade.orderStatus.status = "Cancelled"
            asyncio.get_running_loop().call_soon(trade.statusEvent, trade)

        ib.cancelOrder.side_effect = _cancel
        result = await transport.cancel_order(handle)
        ib.cancelOrder.assert_called_once_with(trade.order)
        assert result.raw_status == "Cancelled"


class TestAC6CancelAll:
    """AC6: cancel_all cancels only owned orders, never a global cancel."""

    async def test_cancels_only_owned_and_returns_handles(self) -> None:
        ib = _mock_ib()
        own1, other, own2 = (
            _trade(1, OWN_CLIENT_ID),
            _trade(2, OTHER_CLIENT_ID),
            _trade(3, OWN_CLIENT_ID),
        )
        ib.openTrades.return_value = [own1, other, own2]

        def _cancel(order: object) -> None:
            for t in (own1, own2):
                if t.order is order:
                    t.orderStatus.status = "Cancelled"
                    asyncio.get_running_loop().call_soon(t.statusEvent, t)

        ib.cancelOrder.side_effect = _cancel
        result = await _transport(ib).cancel_all()

        cancelled_orders = [c.args[0] for c in ib.cancelOrder.call_args_list]
        assert cancelled_orders == [own1.order, own2.order]
        ib.reqGlobalCancel.assert_not_called()
        assert all(isinstance(h, OrderHandle) for h in result)
        assert sorted(h.order_id for h in result) == [1, 3]

    async def test_no_owned_orders_cancels_nothing(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [_trade(2, OTHER_CLIENT_ID)]
        result = await _transport(ib).cancel_all()
        assert result == []
        ib.cancelOrder.assert_not_called()
        ib.reqGlobalCancel.assert_not_called()

    async def test_raises_when_not_connected(self) -> None:
        transport = _transport(_mock_ib())
        transport._error_router._is_connected = False
        with pytest.raises(ConnectionLostError):
            await transport.cancel_all()

    async def test_gateway_delegates_to_transport(self) -> None:
        transport = MagicMock()
        sentinel = [MagicMock()]
        transport.cancel_all = AsyncMock(return_value=sentinel)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.cancel_all() is sentinel
        transport.cancel_all.assert_awaited_once_with()


class TestAC7Docs:
    """AC7: README client-id section and spec docstring."""

    @staticmethod
    def _client_id_section() -> str:
        readme = Path(__file__).parents[3] / (
            "market_connector/exchanges/interactive_brokers/README.md"
        )
        text = readme.read_text(encoding="utf-8")
        match = re.search(
            r"^#+[ \t]+[^\n]*client[ _-]?id[^\n]*\n(.*?)(?=^#{1,3}[ \t]|\Z)",
            text,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        assert match, "README has no heading mentioning 'client id'"
        return match.group(1).lower()

    def test_readme_client_id_section_content(self) -> None:
        section = self._client_id_section()
        assert "tws" in section
        assert "0" in section
        assert "collide" in section or "collision" in section

    def test_spec_docstring_mentions_fetch_all_open_orders(self) -> None:
        assert "fetch_all_open_orders" in (IbConnectionSpec.__doc__ or "")
