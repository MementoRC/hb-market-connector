"""Unit tests for the #308 unclaimed-rejection record (no ib_async needed)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from market_connector.exchanges.interactive_brokers._error_router import (
    _MAX_UNCLAIMED_REJECTIONS,
    _ErrorRouter,
)
from market_connector.exchanges.interactive_brokers.exceptions import OrderRejectedError
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


class _Event:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _Event:
        self.handlers.append(handler)
        return self

    def __isub__(self, handler: Any) -> _Event:
        self.handlers.remove(handler)
        return self


def test_rejection_without_waiter_is_recorded() -> None:
    router = _ErrorRouter()

    router.on_error(req_id=7, code=201, msg="rejected")

    assert isinstance(router._unclaimed_order_rejections[7], OrderRejectedError)


async def test_rejection_with_waiter_is_not_recorded() -> None:
    router = _ErrorRouter()
    router._pending_order_waiters[7] = asyncio.get_running_loop().create_future()

    router.on_error(req_id=7, code=201, msg="rejected")

    assert 7 not in router._unclaimed_order_rejections


def test_record_is_capped_and_evicts_oldest() -> None:
    router = _ErrorRouter()

    for order_id in range(_MAX_UNCLAIMED_REJECTIONS + 5):
        router.on_error(req_id=order_id, code=201, msg="rejected")

    assert len(router._unclaimed_order_rejections) == _MAX_UNCLAIMED_REJECTIONS
    assert 0 not in router._unclaimed_order_rejections
    assert _MAX_UNCLAIMED_REJECTIONS + 4 in router._unclaimed_order_rejections


async def test_waiter_claims_recorded_rejection_and_cleans_up() -> None:
    router = _ErrorRouter()
    router.on_error(req_id=9, code=201, msg="rejected")
    trade = SimpleNamespace(order=SimpleNamespace(orderId=9), statusEvent=_Event())
    fake_self = SimpleNamespace(
        _error_router=router, _first_status_timeout=0.5, _spec=SimpleNamespace(client_id=1)
    )

    recorded = router._unclaimed_order_rejections[9]

    with pytest.raises(OrderRejectedError) as excinfo:
        await IbGatewayTransport._wait_first_status_update(fake_self, trade)  # type: ignore[arg-type]

    assert excinfo.value is recorded
    assert 9 not in router._unclaimed_order_rejections
    assert not router._pending_order_waiters
    assert trade.statusEvent.handlers == []
