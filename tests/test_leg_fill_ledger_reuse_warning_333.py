"""RED acceptance tests for #333: ``LegFillLedger.record`` makes tag reuse visible.

Contract: link tags are unique per placement. ``record`` takes an optional opaque
``placement_id`` (IB: permId, else orderId). The ledger remembers the last placement_id seen per
client_order_id. A different non-None placement_id for the same tag logs exactly one WARNING
(tag, old and new ids), whether or not the fill is accepted. A lower or equal cumulative from
the SAME placement (replay, busted-trade correction) is dropped silently, as is anything when
``placement_id`` is None. Accept/drop behaviour and return values are unchanged.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.leg_fill_hub import LegFillLedger
from market_connector.protocols import LegFill

if TYPE_CHECKING:
    import pytest

_LOGGER = "market_connector.leg_fill_hub"
_CID = "mcl-7-0"


def _fill(cum: str, fill_id: str = "f") -> LegFill:
    return LegFill(
        client_order_id=_CID,
        cumulative_filled=Decimal(cum),
        last_price=Decimal(100),
        timestamp=1.0,
        fill_id=fill_id,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _LOGGER]


def test_different_placement_lower_cumulative_warns_once_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("7", "b"), placement_id="perm-2")

    assert accepted is False
    (rec,) = _warnings(caplog)
    assert rec.levelno == logging.WARNING
    message = rec.getMessage()
    assert _CID in message
    assert "perm-1" in message
    assert "perm-2" in message
    assert "link tag reused across placements" in message
    assert "#333" in message
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]


def test_different_placement_greater_cumulative_warns_once_and_accepts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("4", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"), placement_id="perm-2")

    assert accepted is True
    (rec,) = _warnings(caplog)
    message = rec.getMessage()
    assert _CID in message
    assert "perm-1" in message
    assert "perm-2" in message
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]


def test_same_placement_lower_cumulative_drops_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("5", "b"), placement_id="perm-1")

    assert accepted is False
    assert _warnings(caplog) == []
    assert [f.fill_id for f in ledger.latest()] == ["a"]


def test_no_placement_id_lower_cumulative_drops_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a")) is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("7", "b"))

    assert accepted is False
    assert _warnings(caplog) == []
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]


def test_none_placement_id_after_known_one_never_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        lower = ledger.record(_fill("7", "b"), placement_id=None)
        equal = ledger.record(_fill("10", "c"), placement_id=None)

    assert (lower, equal) == (False, False)
    assert _warnings(caplog) == []


def test_equal_cumulative_same_placement_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"), placement_id="perm-1")

    assert accepted is False
    assert _warnings(caplog) == []
    assert [f.fill_id for f in ledger.latest()] == ["a"]


def test_greater_cumulative_same_placement_is_accepted_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("4", "a"), placement_id="perm-1") is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"), placement_id="perm-1")

    assert accepted is True
    assert _warnings(caplog) == []
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]


def test_greater_cumulative_without_placement_id_is_accepted_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("4", "a")) is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"))

    assert accepted is True
    assert _warnings(caplog) == []
