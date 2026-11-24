"""Review regression for #326: a permId record wins over a shared orderRef record."""

from __future__ import annotations

from decimal import Decimal

import pytest
from data_type_primitives import OrderState

from ._doubles import make_fill
from .test_leg_filled_fallback_326 import ORDER_ID, SYMBOL, _build, _live_exec, _open

pytest.importorskip("ib_async")

PERM_A = 111
PERM_B = 222
SHARED_TAG = "tagX"


async def test_shared_order_ref_does_not_override_perm_id_match() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    trade = _open(0.0, client_id=own, order_ref=SHARED_TAG)
    trade.order.permId = PERM_A
    ib.open_list = [trade]
    _live_exec(ib, "a1.01", 4, perm_id=PERM_A, order_ref=SHARED_TAG)
    ib.execDetailsEvent(
        None, make_fill("b1.01", cum_qty=10, shares=10, perm_id=PERM_B, order_ref=SHARED_TAG)
    )

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert snap.filled == Decimal(4)
    assert snap.state is not OrderState.FILLED


async def test_perm_id_without_record_ignores_shared_order_ref_record() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    trade = _open(0.0, client_id=own, order_ref=SHARED_TAG)
    trade.order.permId = PERM_A  # no execution carries PERM_A
    ib.open_list = [trade]
    ib.execDetailsEvent(
        None, make_fill("b1.01", cum_qty=10, shares=10, perm_id=PERM_B, order_ref=SHARED_TAG)
    )

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert snap.filled == Decimal(0)
    assert snap.state is not OrderState.FILLED


async def test_order_id_fallback_when_perm_id_and_order_ref_empty() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    trade = _open(0.0, client_id=own, order_ref="")
    trade.order.permId = 0
    ib.open_list = [trade]
    fill = make_fill("c1.01", cum_qty=10, shares=10, perm_id=0, order_ref="", order_id=ORDER_ID)
    fill.execution.clientId = own
    ib.execDetailsEvent(None, fill)

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert (snap.state, snap.filled) == (OrderState.FILLED, Decimal(10))


async def test_order_id_fallback_ignores_other_clients_executions() -> None:
    gateway, ib = await _build()
    own = gateway._transport.client_id  # noqa: SLF001
    trade = _open(0.0, client_id=own, order_ref="")
    trade.order.permId = 0
    ib.open_list = [trade]
    fill = make_fill("d1.01", cum_qty=10, shares=10, perm_id=0, order_ref="", order_id=ORDER_ID)
    fill.execution.clientId = own + 1
    ib.execDetailsEvent(None, fill)

    snap = await gateway.get_leg(SYMBOL, client_order_id=str(ORDER_ID))

    assert snap is not None
    assert snap.filled == Decimal(0)
