"""RED acceptance tests for #326: IB leg ``filled`` falls back to observed execution cumQty.

Live evidence: completed orders can report ``orderStatus.filled == 0`` (and even status
Cancelled/Inactive) although executions filled them. ``get_leg`` / ``list_open_legs`` must report
``filled = max(orderStatus.filled or 0, highest Execution.cumQty observed by the transport)``,
matching executions by permId (or by orderRef when permId is unavailable). When the corrected
filled reaches the order's totalQuantity the state is FILLED regardless of the venue status.
No observed executions and no venue fill never synthesize a fill.
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

from ._doubles import MockIb, make_fill

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1"
SYMBOL = "AAPL"
PERM = 1234567
ORDER_ID = 42
TAG = "mcl-5-0"
QTY = 10.0


class LegIb(MockIb):
    """MockIb serving configurable open and completed trades."""

    def __init__(self) -> None:
        super().__init__()
        self.open_list: list[Any] = []
        self.completed_list: list[Any] = []

    def openTrades(self) -> list:  # noqa: N802
        return list(self.open_list)

    async def reqCompletedOrdersAsync(self, apiOnly: bool = False) -> list:  # noqa: N802, N803
        return list(self.completed_list)


def _trade(  # noqa: PLR0913
    *,
    status: str,
    filled: float | None,
    perm_id: int = PERM,
    order_id: int = 0,
    client_id: int = 0,
    order_ref: str = "",
    total: float = QTY,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=SYMBOL, conId=1),
        order=SimpleNamespace(
            orderId=order_id,
            clientId=client_id,
            permId=perm_id,
            orderRef=order_ref,
            account=ACCT,
            totalQuantity=total,
        ),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0),
        fills=[],
    )


def _completed(status: str, filled: float | None, **kw: Any) -> SimpleNamespace:
    """Live shape: orderId=0, clientId=0, empty orderRef, real permId."""
    return _trade(status=status, filled=filled, **kw)


def _open(filled: float | None, *, client_id: int, order_ref: str = TAG) -> SimpleNamespace:
    return _trade(
        status="Submitted",
        filled=filled,
        order_id=ORDER_ID,
        client_id=client_id,
        order_ref=order_ref,
    )


async def _build(
    *,
    completed: list[Any] | None = None,
    open_trades: list[Any] | None = None,
    reconciled: list[Any] | None = None,
) -> tuple[IbGatewayGateway, LegIb]:
    ib = LegIb()
    ib.managed_accounts = [ACCT]
    ib.completed_list = list(completed or [])
    ib.open_list = list(open_trades or [])
    ib.executions_result = list(reconciled or [])  # served by the reconcile on connect
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(
            IbConnectionSpec(account_id=ACCT, fetch_all_open_orders=False)
        )
    await transport.connect()
    native = SimpleNamespace(symbol=SYMBOL, secType="STK", conId=1)
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=SimpleNamespace(native=native))
    resolver.resolve_from_pair = AsyncMock(return_value=SimpleNamespace(native=native))
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, ib


def _live_exec(ib: LegIb, exec_id: str, cum: float, **kw: Any) -> None:
    ib.execDetailsEvent(None, make_fill(exec_id, cum_qty=cum, shares=cum, **kw))


async def _get_completed(gateway: IbGatewayGateway, ib: LegIb) -> Any:
    """Record PERM for ORDER_ID while open, then look the (now completed) leg up by id."""
    own = gateway._transport.client_id  # noqa: SLF001
    ib.openOrderEvent(_open(0.0, client_id=own))
    return await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))


async def test_filled_status_with_zero_order_status_filled_uses_observed_cum_qty() -> None:
    gateway, ib = await _build(completed=[_completed("Filled", 0.0)])
    _live_exec(ib, "e1.01", 4, perm_id=PERM)
    _live_exec(ib, "e2.01", 10, perm_id=PERM)

    snap = await _get_completed(gateway, ib)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.FILLED, Decimal(10))


@pytest.mark.parametrize("venue_status", ["Cancelled", "Inactive"])
async def test_venue_cancelled_but_observed_full_qty_is_filled(venue_status: str) -> None:
    gateway, ib = await _build(completed=[_completed(venue_status, 0.0)])
    _live_exec(ib, "e1.01", 10, perm_id=PERM)

    snap = await _get_completed(gateway, ib)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.FILLED, Decimal(10))


async def test_venue_cancelled_with_observed_partial_stays_canceled_with_filled() -> None:
    gateway, ib = await _build(completed=[_completed("Cancelled", 0.0)])
    _live_exec(ib, "e1.01", 4, perm_id=PERM)

    snap = await _get_completed(gateway, ib)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.CANCELED, Decimal(4))


async def test_genuine_cancel_without_executions_is_not_synthesized_as_filled() -> None:
    gateway, ib = await _build(completed=[_completed("Cancelled", 0.0)])

    snap = await _get_completed(gateway, ib)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.CANCELED, Decimal(0))


async def test_open_trade_filled_lagging_observed_cum_qty_is_corrected() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    ib.open_list = [_open(3.0, client_id=own)]
    _live_exec(ib, "e1.01", 7, perm_id=PERM)

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))
    legs = await gateway.list_open_legs(SYMBOL)

    assert snap is not None
    assert snap.filled == Decimal(7)
    assert [leg.filled for leg in legs] == [Decimal(7)]


async def test_none_order_status_filled_and_nothing_observed_is_zero() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    ib.open_list = [_open(None, client_id=own)]

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert snap.filled == Decimal(0)
    assert isinstance(snap.filled, Decimal)


async def test_restart_executions_via_reconcile_only_matched_by_perm_id_and_order_ref() -> None:
    reconciled = [
        make_fill("r1.01", cum_qty=4, shares=4, perm_id=PERM, order_ref=TAG),
        make_fill("r2.01", cum_qty=10, shares=6, perm_id=PERM, order_ref=TAG),
    ]
    gateway, _ = await _build(
        completed=[_completed("Cancelled", 0.0, order_ref=TAG)], reconciled=reconciled
    )

    snap = await gateway.get_leg(SYMBOL, link_tag=TAG)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.FILLED, Decimal(10))


async def test_order_status_filled_above_observed_cum_qty_is_never_lowered() -> None:
    gateway, ib = await _build(completed=[_completed("Cancelled", 6.0)])
    _live_exec(ib, "e1.01", 4, perm_id=PERM)

    snap = await _get_completed(gateway, ib)

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.CANCELED, Decimal(6))
