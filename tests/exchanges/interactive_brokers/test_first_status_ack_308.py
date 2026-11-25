"""RED acceptance tests for #308 (Interactive Brokers): an already-acked leg must not time out.

Contract: ``place_order``, ``place_bracket`` and ``place_oca`` / ``place_oco`` resolve as soon
as every leg's trade is acknowledged by IB, whenever that acknowledgement happened:

* the status was already set (``Submitted`` / ``PreSubmitted``) synchronously inside
  ``placeOrder`` and no later ``statusEvent`` fires, or
* ``statusEvent`` fired synchronously inside ``placeOrder`` (before any waiter subscribed), or
* it arrives later (regression guard: the delayed-ack pattern of the #250 tests).

A synchronous rejection (IB error 201 on the order id while ``placeOrder`` runs, status
``Inactive``) surfaces as ``OrderRejectedError`` without waiting for the first-status timeout.

Every call is bounded with ``asyncio.wait_for`` and the transport timeout is short, so a
regression fails fast instead of hanging CI.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import OrderRejectedError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb
from .test_order_ref_link_tag_250 import AckIb, _bracket, _limit, _oco

if TYPE_CHECKING:
    from collections.abc import Awaitable

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1111111"
_TIMEOUT = 0.5  # transport first-status timeout
_BOUND = 2.0  # outer guard: only reached if the transport timeout itself fails to fire
_FAST = 0.3  # a correct implementation resolves well below the transport timeout

class SyncStatusIb(MockIb):
    """Sets the trade status synchronously inside placeOrder; no statusEvent ever fires."""

    status = "Submitted"

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        trade = super().placeOrder(contract, order)
        trade.orderStatus.status = self.status
        return trade


class SyncPreSubmittedIb(SyncStatusIb):
    status = "PreSubmitted"


class SyncEventIb(MockIb):
    """Fires statusEvent synchronously inside placeOrder; only handlers attached by then run."""

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        trade = super().placeOrder(contract, order)
        self.emit_status(trade.order.orderId, "Submitted")
        return trade


class SyncRejectIb(MockIb):
    """Rejects synchronously inside placeOrder: status ``Inactive`` plus IB error 201."""

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        trade = super().placeOrder(contract, order)
        trade.orderStatus.status = "Inactive"
        self.emit_error(trade.order.orderId, 201, "order rejected")
        return trade


async def _gateway(ib: MockIb) -> IbGatewayGateway:
    ib.managed_accounts = [ACCT]
    ib.client.next_req_id = 500
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = _TIMEOUT
    native = SimpleNamespace(symbol="AAPL", secType="STK", conId=1)
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=SimpleNamespace(native=native))
    resolver.resolve_from_pair = AsyncMock(return_value=SimpleNamespace(native=native))
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)


async def _timed[T](coro: Awaitable[T]) -> tuple[T, float]:
    start = time.monotonic()
    result = await asyncio.wait_for(coro, timeout=_BOUND)
    return result, time.monotonic() - start


ACKED_IBS = [SyncStatusIb, SyncPreSubmittedIb, SyncEventIb]


@pytest.mark.parametrize("ib_cls", ACKED_IBS)
class TestAlreadyAcked:
    async def test_single_place_order_resolves_before_timeout(self, ib_cls: type[MockIb]) -> None:
        gateway = await _gateway(ib_cls())

        _, elapsed = await _timed(gateway.place_order(MagicMock(), _limit()))

        assert elapsed < _FAST

    async def test_place_bracket_resolves_before_timeout(self, ib_cls: type[MockIb]) -> None:
        ib = ib_cls()
        gateway = await _gateway(ib)

        _, elapsed = await _timed(gateway.place_bracket("AAPL", _bracket()))

        assert elapsed < _FAST
        assert len(ib.placed_orders) == 3

    async def test_place_oco_resolves_before_timeout(self, ib_cls: type[MockIb]) -> None:
        ib = ib_cls()
        gateway = await _gateway(ib)

        _, elapsed = await _timed(gateway.place_oco("AAPL", _oco()))

        assert elapsed < _FAST
        assert len(ib.placed_orders) == 2

    async def test_transport_place_oca_resolves_before_timeout(self, ib_cls: type[MockIb]) -> None:
        gateway = await _gateway(ib_cls())
        transport = gateway._transport

        _, elapsed = await _timed(transport.place_oca(MagicMock(), [_limit(), _limit("101")]))

        assert elapsed < _FAST


class TestSynchronousRejection:
    async def test_single_place_order_raises_rejected_without_waiting(self) -> None:
        gateway = await _gateway(SyncRejectIb())
        start = time.monotonic()

        with pytest.raises(OrderRejectedError):
            await asyncio.wait_for(gateway.place_order(MagicMock(), _limit()), timeout=_BOUND)

        assert time.monotonic() - start < _FAST

    async def test_place_bracket_raises_rejected_without_waiting(self) -> None:
        gateway = await _gateway(SyncRejectIb())
        start = time.monotonic()

        with pytest.raises(OrderRejectedError):
            await asyncio.wait_for(gateway.place_bracket("AAPL", _bracket()), timeout=_BOUND)

        assert time.monotonic() - start < _FAST


class TestDelayedAckRegression:
    async def test_single_place_order_with_delayed_ack(self) -> None:
        gateway = await _gateway(AckIb())

        _, elapsed = await _timed(gateway.place_order(MagicMock(), _limit()))

        assert elapsed < _TIMEOUT

    async def test_place_bracket_with_delayed_ack(self) -> None:
        ib = AckIb()
        gateway = await _gateway(ib)

        _, elapsed = await _timed(gateway.place_bracket("AAPL", _bracket()))

        assert elapsed < _TIMEOUT
        assert len(ib.placed_orders) == 3

    async def test_place_oco_with_delayed_ack(self) -> None:
        ib = AckIb()
        gateway = await _gateway(ib)

        _, elapsed = await _timed(gateway.place_oco("AAPL", _oco()))

        assert elapsed < _TIMEOUT
        assert len(ib.placed_orders) == 2
