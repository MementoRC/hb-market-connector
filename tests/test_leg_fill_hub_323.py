"""RED acceptance tests for #323: the shared ``LegFillLedger`` and the protocol wording.

Contract: ``market_connector.leg_fill_hub.LegFillLedger(max_entries)`` remembers the latest
``LegFill`` per ``client_order_id``.

* ``record(fill) -> bool`` is True only when ``cumulative_filled`` strictly increases for that
  ``client_order_id``; equal or lower values are rejected (False) and do not replace the entry.
* ``latest()`` yields the latest accepted ``LegFill`` of every leg.
* It is bounded: past ``max_entries`` the oldest leg is evicted first (best effort; an evicted
  leg is simply not replayed).
* ``SupportsLegFills.subscribe_leg_fills`` documents that a new subscriber receives the latest
  known cumulative per leg.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any

from market_connector.protocols import LegFill, SupportsLegFills

_D = Decimal


def _ledger(max_entries: int = 8) -> Any:
    module = importlib.import_module("market_connector.leg_fill_hub")
    return module.LegFillLedger(max_entries)


def _fill(cid: str, cum: str, fill_id: str | None = None) -> LegFill:
    return LegFill(
        client_order_id=cid,
        cumulative_filled=_D(cum),
        last_price=_D("100"),
        timestamp=1.0,
        fill_id=fill_id,
    )


def _by_cid(ledger: Any) -> dict[str, LegFill]:
    return {f.client_order_id: f for f in ledger.latest()}


class TestRecord:
    def test_first_fill_of_a_leg_is_accepted(self) -> None:
        assert _ledger().record(_fill("a", "1")) is True

    def test_strictly_higher_cumulative_is_accepted(self) -> None:
        ledger = _ledger()
        ledger.record(_fill("a", "1"))

        assert ledger.record(_fill("a", "2")) is True

    def test_equal_cumulative_is_rejected(self) -> None:
        ledger = _ledger()
        ledger.record(_fill("a", "1", "t1"))

        assert ledger.record(_fill("a", "1", "t2")) is False
        assert _by_cid(ledger)["a"].fill_id == "t1"

    def test_lower_cumulative_is_rejected(self) -> None:
        ledger = _ledger()
        ledger.record(_fill("a", "2", "t2"))

        assert ledger.record(_fill("a", "1", "t1")) is False
        assert _by_cid(ledger)["a"].cumulative_filled == _D("2")

    def test_legs_are_tracked_independently(self) -> None:
        ledger = _ledger()
        ledger.record(_fill("a", "5"))

        assert ledger.record(_fill("b", "1")) is True


class TestLatest:
    def test_yields_the_latest_fill_per_leg(self) -> None:
        ledger = _ledger()
        ledger.record(_fill("a", "1", "a1"))
        ledger.record(_fill("b", "3", "b1"))
        ledger.record(_fill("a", "2", "a2"))

        latest = _by_cid(ledger)

        assert set(latest) == {"a", "b"}
        assert latest["a"].cumulative_filled == _D("2")
        assert latest["a"].fill_id == "a2"
        assert latest["b"].fill_id == "b1"

    def test_empty_ledger_yields_nothing(self) -> None:
        assert list(_ledger().latest()) == []


class TestBounded:
    def test_oldest_leg_is_evicted_first_past_max_entries(self) -> None:
        ledger = _ledger(max_entries=2)
        ledger.record(_fill("a", "1"))
        ledger.record(_fill("b", "1"))
        ledger.record(_fill("c", "1"))

        assert set(_by_cid(ledger)) == {"b", "c"}

    def test_never_holds_more_than_max_entries(self) -> None:
        ledger = _ledger(max_entries=3)
        for n in range(10):
            ledger.record(_fill(f"leg-{n}", "1"))

        assert len(list(ledger.latest())) == 3

    def test_evicted_leg_is_not_replayed_but_newer_ones_are(self) -> None:
        ledger = _ledger(max_entries=1)
        ledger.record(_fill("a", "1"))
        ledger.record(_fill("b", "1"))

        assert [f.client_order_id for f in ledger.latest()] == ["b"]


class TestProtocolDocumentation:
    def test_subscribe_leg_fills_docstring_states_latest_known_replay(self) -> None:
        doc = SupportsLegFills.subscribe_leg_fills.__doc__ or ""

        assert "latest known" in doc.lower()
