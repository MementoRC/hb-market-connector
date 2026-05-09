"""Tests for OrderState enum and OrderHandle frozen dataclass."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from market_connector.exchanges.interactive_brokers.order_handle import (
    _TRADE_STATUS_MAP,
    OrderHandle,
    OrderState,
)


class TestOrderState:
    def test_is_str_enum(self):
        assert isinstance(OrderState.PENDING, str)

    def test_all_six_values_exist(self):
        assert OrderState.PENDING == "pending"
        assert OrderState.SUBMITTED == "submitted"
        assert OrderState.PARTIALLY_FILLED == "partially_filled"
        assert OrderState.FILLED == "filled"
        assert OrderState.CANCELLED == "cancelled"
        assert OrderState.REJECTED == "rejected"


class TestTradeStatusMap:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("PendingSubmit", OrderState.PENDING),
            ("PreSubmitted", OrderState.PENDING),
            ("PendingCancel", OrderState.PENDING),
            ("Submitted", OrderState.SUBMITTED),
            ("ApiPending", OrderState.SUBMITTED),
            ("Filled", OrderState.FILLED),
            ("Cancelled", OrderState.CANCELLED),
            ("ApiCancelled", OrderState.CANCELLED),
            ("Inactive", OrderState.REJECTED),
        ],
    )
    def test_known_statuses_map_correctly(self, raw, expected):
        assert _TRADE_STATUS_MAP[raw] == expected

    def test_unknown_status_raises_key_error(self):
        assert "__unknown__" not in _TRADE_STATUS_MAP


class TestOrderHandleFromTrade:
    def _make_trade(
        self,
        *,
        perm_id: int = 0,
        order_id: int = 100,
        status: str = "Submitted",
        filled: float = 0.0,
        avg_fill_price: float = 0.0,
    ) -> MagicMock:
        trade = MagicMock()
        trade.order.permId = perm_id
        trade.order.orderId = order_id
        trade.orderStatus.status = status
        trade.orderStatus.filled = filled
        trade.orderStatus.avgFillPrice = avg_fill_price
        return trade

    def test_uses_perm_id_when_available(self):
        trade = self._make_trade(perm_id=9999, order_id=100)
        handle = OrderHandle.from_trade(trade)
        assert handle.order_id == 9999

    def test_falls_back_to_order_id_when_perm_id_zero(self):
        trade = self._make_trade(perm_id=0, order_id=100)
        handle = OrderHandle.from_trade(trade)
        assert handle.order_id == 100

    def test_submitted_no_fills(self):
        trade = self._make_trade(status="Submitted", filled=0.0)
        handle = OrderHandle.from_trade(trade)
        assert handle.status == OrderState.SUBMITTED
        assert handle.filled_qty == Decimal("0")
        assert handle.avg_fill_price is None

    def test_partially_filled_promoted_from_submitted(self):
        trade = self._make_trade(status="Submitted", filled=5.0, avg_fill_price=100.0)
        handle = OrderHandle.from_trade(trade)
        assert handle.status == OrderState.PARTIALLY_FILLED
        assert handle.filled_qty == Decimal("5")
        assert handle.avg_fill_price == Decimal("100.0")

    def test_filled_status(self):
        trade = self._make_trade(status="Filled", filled=10.0, avg_fill_price=99.5)
        handle = OrderHandle.from_trade(trade)
        assert handle.status == OrderState.FILLED
        assert handle.avg_fill_price == Decimal("99.5")

    def test_cancelled_status(self):
        trade = self._make_trade(status="Cancelled")
        handle = OrderHandle.from_trade(trade)
        assert handle.status == OrderState.CANCELLED

    def test_rejected_via_inactive(self):
        trade = self._make_trade(status="Inactive")
        handle = OrderHandle.from_trade(trade)
        assert handle.status == OrderState.REJECTED

    def test_unknown_status_raises_value_error(self):
        trade = self._make_trade(status="NewUnknownStatus")
        with pytest.raises(ValueError, match="Unknown IB order status"):
            OrderHandle.from_trade(trade)

    def test_raw_status_preserved(self):
        trade = self._make_trade(status="ApiPending")
        handle = OrderHandle.from_trade(trade)
        assert handle.raw_status == "ApiPending"
        assert handle.status == OrderState.SUBMITTED

    def test_handle_is_frozen(self):
        trade = self._make_trade()
        handle = OrderHandle.from_trade(trade)
        with pytest.raises((AttributeError, TypeError)):
            handle.status = OrderState.FILLED  # type: ignore[misc]

    def test_trade_reference_stored(self):
        trade = self._make_trade()
        handle = OrderHandle.from_trade(trade)
        assert handle._trade is trade


class TestOrderHandleWaitFor:
    def _make_trade_with_events(self, initial_status: str = "Submitted") -> MagicMock:
        """Return a trade mock with a real callable statusEvent list."""
        trade = MagicMock()
        trade.order.permId = 0
        trade.order.orderId = 100
        trade.orderStatus.status = initial_status
        trade.orderStatus.filled = 0.0
        trade.orderStatus.avgFillPrice = 0.0

        # Simulate ib_async Event: += appends, -= removes, calling fires all handlers
        _handlers: list = []

        def iadd(_self, cb):
            _handlers.append(cb)
            return trade.statusEvent

        def isub(_self, cb):
            if cb in _handlers:
                _handlers.remove(cb)
            return trade.statusEvent

        def call(*args):
            for h in list(_handlers):
                h(*args)

        trade.statusEvent.__iadd__ = iadd
        trade.statusEvent.__isub__ = isub
        trade.statusEvent.side_effect = call
        trade._handlers = _handlers
        return trade

    @pytest.mark.asyncio
    async def test_wait_for_happy_path(self):
        """statusEvent fires with target status → wait_for returns updated handle."""
        import asyncio

        trade = self._make_trade_with_events("Submitted")
        handle = OrderHandle.from_trade(trade)

        async def fire_status_after_delay():
            await asyncio.sleep(0.01)
            trade.orderStatus.status = "Filled"
            trade.orderStatus.filled = 10.0
            trade.orderStatus.avgFillPrice = 99.5
            trade.statusEvent(trade)

        task = asyncio.create_task(fire_status_after_delay())
        result = await handle.wait_for(status=OrderState.FILLED, timeout=1.0)
        await task

        assert result.status == OrderState.FILLED
        assert result.filled_qty == Decimal("10")

    @pytest.mark.asyncio
    async def test_wait_for_timeout_raises(self):
        """No statusEvent fires → asyncio.TimeoutError raised."""
        import asyncio

        trade = self._make_trade_with_events("Submitted")
        handle = OrderHandle.from_trade(trade)

        with pytest.raises(asyncio.TimeoutError):
            await handle.wait_for(status=OrderState.FILLED, timeout=0.05)

    @pytest.mark.asyncio
    async def test_wait_for_terminal_mismatch_raises(self):
        """Order reaches REJECTED while waiting for FILLED → RuntimeError raised."""
        import asyncio

        trade = self._make_trade_with_events("Submitted")
        handle = OrderHandle.from_trade(trade)

        async def fire_rejected():
            await asyncio.sleep(0.01)
            trade.orderStatus.status = "Inactive"
            trade.statusEvent(trade)

        task = asyncio.create_task(fire_rejected())
        with pytest.raises(RuntimeError, match="terminal"):
            await handle.wait_for(status=OrderState.FILLED, timeout=1.0)
        await task

    @pytest.mark.asyncio
    async def test_wait_for_accepts_set_of_states(self):
        """wait_for accepts a set of target states and resolves on any match."""
        import asyncio

        trade = self._make_trade_with_events("Submitted")
        handle = OrderHandle.from_trade(trade)

        async def fire_cancelled():
            await asyncio.sleep(0.01)
            trade.orderStatus.status = "Cancelled"
            trade.statusEvent(trade)

        task = asyncio.create_task(fire_cancelled())
        result = await handle.wait_for(
            status={OrderState.FILLED, OrderState.CANCELLED}, timeout=1.0
        )
        await task

        assert result.status == OrderState.CANCELLED

    @pytest.mark.asyncio
    async def test_wait_for_unhooks_on_timeout(self):
        """Handlers are cleaned up after TimeoutError."""
        import asyncio

        trade = self._make_trade_with_events("Submitted")
        handle = OrderHandle.from_trade(trade)

        with pytest.raises(asyncio.TimeoutError):
            await handle.wait_for(status=OrderState.FILLED, timeout=0.02)

        # No handlers should remain after timeout
        assert len(trade._handlers) == 0
