"""RED acceptance test for #333 (Interactive Brokers): tag reuse is visible as a WARNING.

Two placements reuse the same link tag; the second one's cumulative is lower than the first's
last. The IB leg-fill path drops it (no ``LegFill`` emitted) and must log a WARNING.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from .test_leg_fills_249 import TAG_A, _exec, _gateway, _place

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

pytest.importorskip("ib_async")

_LOGGER = "market_connector.leg_fill_hub"


async def test_reused_tag_with_lower_cumulative_warns_and_emits_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, ib = await _gateway()
    first = await _place(gateway, ib, TAG_A)
    got: list[LegFill] = []
    gateway.subscribe_leg_fills(got.append)
    _exec(ib, "e1.01", first, 10, tag=TAG_A)
    second = await _place(gateway, ib, TAG_A)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        _exec(ib, "e2.01", second, 3, tag=TAG_A)

    assert [f.cumulative_filled for f in got] == [Decimal(10)]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _LOGGER]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert TAG_A in message
    assert "10" in message
    assert "3" in message
