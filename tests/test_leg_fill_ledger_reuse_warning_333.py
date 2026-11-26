"""RED acceptance tests for #333: ``LegFillLedger.record`` makes a reused-tag drop visible.

Contract: link tags are unique per placement. A fill dropped because its cumulative is
strictly LOWER than the last recorded one signals tag reuse and logs exactly one WARNING
(client_order_id, previous and dropped cumulative). An EQUAL cumulative is a benign duplicate
(redelivery/replay): no WARNING or above. Return values and ledger state are unchanged.
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
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_lower_cumulative_drop_logs_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a")) is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("7", "b"))

    assert accepted is False
    (rec,) = _warnings(caplog)
    assert rec.levelno == logging.WARNING
    assert rec.name == _LOGGER
    message = rec.getMessage()
    assert _CID in message
    assert "10" in message
    assert "7" in message
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]


def test_equal_cumulative_duplicate_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("10", "a")) is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"))

    assert accepted is False
    assert _warnings(caplog) == []
    assert [f.fill_id for f in ledger.latest()] == ["a"]


def test_greater_cumulative_is_accepted_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    ledger = LegFillLedger()
    assert ledger.record(_fill("4", "a")) is True

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accepted = ledger.record(_fill("10", "b"))

    assert accepted is True
    assert _warnings(caplog) == []
    assert [f.cumulative_filled for f in ledger.latest()] == [Decimal(10)]
