"""Acceptance tests for read-only connect handling (issue #151).

Red-phase tests: ``IbReadOnlyApiError`` / ``IbReadOnlyModeError`` do not exist yet.
New names are imported inside each test so tests on existing names still collect.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.exchanges.interactive_brokers import exceptions
from market_connector.exchanges.interactive_brokers.exceptions import IbError
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from .test_connect_retry import _script
from .test_connection_lifecycle import _make

if TYPE_CHECKING:
    from ._doubles import MockIb


def _read_only_api_timeout(*messages: str) -> ConnectionError:
    """What ib_async raises when the Gateway runs with READ_ONLY_API=yes."""
    return ConnectionError(list(messages))


_BOTH = ("open orders request timed out", "completed orders request timed out")


def _arm_order_methods(ib: MockIb) -> dict[str, Any]:
    ib.placeOrder = MagicMock()  # type: ignore[method-assign]
    ib.cancelOrder = MagicMock()  # type: ignore[method-assign]
    ib.reqGlobalCancel = MagicMock()  # type: ignore[attr-defined]
    return {
        "placeOrder": ib.placeOrder,
        "cancelOrder": ib.cancelOrder,
        "reqGlobalCancel": ib.reqGlobalCancel,
    }


class TestReadOnlySpec:
    async def test_connect_passes_readonly_and_skips_all_open_orders(self) -> None:
        transport, ib, _ = _make(IbConnectionSpec(readonly=True, fetch_all_open_orders=True))
        ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])  # type: ignore[attr-defined]
        await transport.connect()
        assert ib.connect_calls[0]["readonly"] is True
        ib.reqAllOpenOrdersAsync.assert_not_called()


class TestReadOnlyOrderGuards:
    async def test_place_order_raises(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            IbReadOnlyModeError,
        )
        from market_connector.orders import HBOrder, OrderType, TradeType  # noqa: PLC0415

        transport, ib, _ = _make(IbConnectionSpec(readonly=True))
        await transport.connect()
        calls = _arm_order_methods(ib)
        order = HBOrder(
            order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(1), price=None
        )
        with pytest.raises(IbReadOnlyModeError):
            await transport.place_order(MagicMock(), order)
        calls["placeOrder"].assert_not_called()

    async def test_cancel_order_raises(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            IbReadOnlyModeError,
        )

        transport, ib, _ = _make(IbConnectionSpec(readonly=True))
        await transport.connect()
        calls = _arm_order_methods(ib)
        with pytest.raises(IbReadOnlyModeError):
            await transport.cancel_order(MagicMock())
        calls["cancelOrder"].assert_not_called()

    async def test_cancel_all_raises(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            IbReadOnlyModeError,
        )

        transport, ib, _ = _make(IbConnectionSpec(readonly=True))
        await transport.connect()
        calls = _arm_order_methods(ib)
        with pytest.raises(IbReadOnlyModeError):
            await transport.cancel_all()
        calls["cancelOrder"].assert_not_called()
        calls["reqGlobalCancel"].assert_not_called()


class TestReadOnlyApiDetection:
    @pytest.mark.parametrize(
        "messages",
        [_BOTH, ("completed orders request timed out",)],
        ids=["open_and_completed", "completed_only"],
    )
    async def test_read_only_api_timeout_raises_without_retry(
        self, messages: tuple[str, ...]
    ) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            IbReadOnlyApiError,
        )

        transport, ib, delays = _make(IbConnectionSpec(readonly=False))
        _script(ib, [_read_only_api_timeout(*messages), None])
        with pytest.raises(IbReadOnlyApiError) as excinfo:
            await transport.connect()
        assert len(ib.connect_calls) == 1
        assert delays == []
        assert "READ_ONLY_API" in str(excinfo.value)
        assert "readonly" in str(excinfo.value)

    async def test_unrelated_retryable_failure_still_retries(self) -> None:
        transport, ib, delays = _make(IbConnectionSpec(connect_attempts=3))
        _script(ib, [TimeoutError(), TimeoutError(), TimeoutError()])
        with pytest.raises(TimeoutError):
            await transport.connect()
        assert len(ib.connect_calls) == 3
        assert len(delays) == 2


class TestExports:
    @pytest.mark.parametrize("name", ["IbReadOnlyApiError", "IbReadOnlyModeError"])
    def test_exported_and_subclass_of_ib_error(self, name: str) -> None:
        cls = getattr(exceptions, name)
        assert issubclass(cls, IbError)
