"""Follow-up acceptance tests for issue #55 (AC8..AC10), written goal-first (red)."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
from tests.exchanges.interactive_brokers.test_open_order_visibility import (
    OWN_CLIENT_ID,
    _mock_ib,
    _trade,
    _transport,
)


@pytest.fixture(autouse=True)
def _patch_ib_async_module():
    """ib_async is an optional extra; inject a MagicMock so lazy imports succeed."""
    with patch.dict(sys.modules, {"ib_async": MagicMock()}):
        yield


def _cancelling(ib: MagicMock, trades: list[MagicMock], failing: MagicMock | None = None) -> None:
    def _cancel(order: object) -> None:
        if failing is not None and failing.order is order:
            raise RuntimeError("reject")
        for t in trades:
            if t.order is order:
                t.orderStatus.status = "Cancelled"
                asyncio.get_running_loop().call_soon(t.statusEvent, t)

    ib.cancelOrder.side_effect = _cancel


class TestAC8ManualOrdersSkipped:
    """AC8: cancel_all skips manual TWS orders; explicit cancel_order still allowed."""

    @staticmethod
    def _setup() -> tuple[MagicMock, list[MagicMock]]:
        ib = _mock_ib()
        trades = [_trade(11, 0), _trade(-5, 0), _trade(0, 0)]
        ib.openTrades.return_value = trades
        _cancelling(ib, trades)
        return ib, trades

    async def test_cancel_all_skips_manual_orders(self) -> None:
        ib, trades = self._setup()
        result = await _transport(ib, client_id=0).cancel_all()
        assert [c.args[0] for c in ib.cancelOrder.call_args_list] == [trades[0].order]
        assert [h.order_id for h in result] == [11]

    def test_manual_orders_still_listed_and_flagged(self) -> None:
        ib, _ = self._setup()
        handles = _transport(ib, client_id=0).open_orders()
        assert [(h.order_id, h.owned, h.manual) for h in handles] == [
            (11, True, False),
            (-5, True, True),
            (0, True, True),
        ]

    async def test_explicit_cancel_of_manual_handle_allowed(self) -> None:
        ib, trades = self._setup()
        transport = _transport(ib, client_id=0)
        manual_handle = transport.open_orders()[1]
        await transport.cancel_order(manual_handle)
        ib.cancelOrder.assert_called_once_with(trades[1].order)

    def test_api_order_on_nonzero_client_not_manual(self) -> None:
        ib = _mock_ib()
        ib.openTrades.return_value = [_trade(3, OWN_CLIENT_ID)]
        (handle,) = _transport(ib).open_orders()
        assert handle.manual is False

    def test_manual_is_independent_of_own_client_id(self) -> None:
        handle = OrderHandle.from_trade(_trade(-2, 0))
        assert handle.manual is True


class TestAC9CancelAllCollectsErrors:
    """AC9: cancel_all attempts every owned order and aggregates failures."""

    @staticmethod
    def _setup() -> tuple[MagicMock, list[MagicMock]]:
        ib = _mock_ib()
        trades = [_trade(1, OWN_CLIENT_ID), _trade(2, OWN_CLIENT_ID), _trade(3, OWN_CLIENT_ID)]
        ib.openTrades.return_value = trades
        _cancelling(ib, trades, failing=trades[1])
        return ib, trades

    async def test_attempts_all_then_raises_cancel_all_error(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import CancelAllError

        ib, trades = self._setup()
        with pytest.raises(CancelAllError) as excinfo:
            await _transport(ib).cancel_all()
        assert [c.args[0] for c in ib.cancelOrder.call_args_list] == [t.order for t in trades]
        err = excinfo.value
        assert isinstance(err, ExceptionGroup)
        assert len(err.exceptions) == 1
        assert isinstance(err.exceptions[0], RuntimeError)
        assert str(err.exceptions[0]) == "reject"
        assert sorted(h.order_id for h in err.cancelled) == [1, 3]
        assert all(isinstance(h, OrderHandle) for h in err.cancelled)

    async def test_message_mentions_failed_order_id(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import CancelAllError

        ib, _ = self._setup()
        with pytest.raises(CancelAllError) as excinfo:
            await _transport(ib).cancel_all()
        assert re.search(r"\b2\b", str(excinfo.value))

    async def test_all_success_returns_list_without_raising(self) -> None:
        ib = _mock_ib()
        trades = [_trade(1, OWN_CLIENT_ID), _trade(3, OWN_CLIENT_ID)]
        ib.openTrades.return_value = trades
        _cancelling(ib, trades)
        result = await _transport(ib).cancel_all()
        assert sorted(h.order_id for h in result) == [1, 3]


class TestAC10Docs:
    """AC10: README documents cross-run cancel limits and manual-order skipping."""

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

    def test_previous_run_orders_cannot_be_cancelled(self) -> None:
        section = self._client_id_section()
        assert "previous" in section
        assert "cannot be cancelled" in section

    def test_cancel_all_skips_manual_tws_orders(self) -> None:
        section = self._client_id_section()
        assert "manual" in section
        assert "cancel_all" in section
        assert "skip" in section
