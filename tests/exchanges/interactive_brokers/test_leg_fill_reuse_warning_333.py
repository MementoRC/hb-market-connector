"""RED acceptance tests for #333 (Interactive Brokers): placement-aware reuse warning.

The IB leg-fill path passes the execution's permId (None while permId is 0) as the ledger's
``placement_id``. Two placements reusing a tag (different permId) log one WARNING when the
second's cumulative is dropped. A lower-cumQty execution of the SAME placement (reconcile
replay, busted-trade correction) is dropped silently. Neither emits a ``LegFill``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from ._doubles import MockIb, make_fill
from .test_leg_fills_249 import TAG_A, _gateway, _place

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

pytest.importorskip("ib_async")

_LOGGER = "market_connector.leg_fill_hub"


def _exec_perm(
    ib: MockIb, exec_id: str, order_id: int, cum: float, *, perm_id: int, tag: str
) -> None:
    """Like ``test_leg_fills_249._exec`` but with an explicit permId."""
    fill = make_fill(
        exec_id,
        order_id=order_id,
        perm_id=perm_id,
        cum_qty=cum,
        shares=cum,
        order_ref=tag,
    )
    ib.execDetailsEvent(None, fill)


def _hub_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _LOGGER]


async def test_reused_tag_across_placements_warns_and_emits_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, ib = await _gateway()
    first = await _place(gateway, ib, TAG_A)
    got: list[LegFill] = []
    gateway.subscribe_leg_fills(got.append)
    _exec_perm(ib, "e1.01", first, 10, perm_id=1001, tag=TAG_A)
    second = await _place(gateway, ib, TAG_A)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        _exec_perm(ib, "e2.01", second, 3, perm_id=2002, tag=TAG_A)

    assert [f.cumulative_filled for f in got] == [Decimal(10)]
    warnings = _hub_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert TAG_A in message
    assert "1001" in message
    assert "2002" in message


async def test_same_order_perm_id_zero_then_assigned_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, ib = await _gateway()
    oid = await _place(gateway, ib, TAG_A)
    got: list[LegFill] = []
    gateway.subscribe_leg_fills(got.append)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        _exec_perm(ib, "e1.01", oid, 5, perm_id=0, tag=TAG_A)
        _exec_perm(ib, "e2.01", oid, 3, perm_id=1001, tag=TAG_A)

    assert [f.cumulative_filled for f in got] == [Decimal(5)]
    assert _hub_warnings(caplog) == []


async def test_same_placement_older_execution_drops_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, ib = await _gateway()
    oid = await _place(gateway, ib, TAG_A)
    got: list[LegFill] = []
    gateway.subscribe_leg_fills(got.append)
    _exec_perm(ib, "e2.01", oid, 10, perm_id=1001, tag=TAG_A)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        # Reconcile replay of an execution missed during a disconnect: older cumQty, new execId.
        _exec_perm(ib, "e1.01", oid, 5, perm_id=1001, tag=TAG_A)

    assert [f.cumulative_filled for f in got] == [Decimal(10)]
    assert _hub_warnings(caplog) == []
