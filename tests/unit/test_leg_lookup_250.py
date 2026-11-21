"""RED contract tests for #250: ``LegSnapshot`` and the ``SupportsLegLookup`` protocol.

Contract (mirrors strategy-framework's EmulationVenue / LegSnapshot, structurally; this package
never imports strategy_framework):

    SupportsLegLookup.get_leg(target, *, client_order_id=None, link_tag=None) -> LegSnapshot | None
    SupportsLegLookup.list_open_legs(target) -> Sequence[LegSnapshot]

    LegSnapshot: frozen dataclass(client_order_id: str, link_tag: str | None,
                                  state: OrderState, filled: Decimal)

Both are exported from ``market_connector`` (defined in ``market_connector.protocols``).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Protocol, runtime_checkable

import pytest
from data_type_primitives import OrderState

import market_connector
from market_connector import protocols


@runtime_checkable
class _SfLegSnapshot(Protocol):
    """Structural stand-in for strategy_framework's LegSnapshot (read-only members)."""

    @property
    def client_order_id(self) -> str: ...

    @property
    def link_tag(self) -> str | None: ...

    @property
    def state(self) -> OrderState: ...

    @property
    def filled(self) -> Decimal: ...


def _snapshot(**overrides: object) -> protocols.LegSnapshot:
    fields: dict[str, object] = {
        "client_order_id": "cid-1",
        "link_tag": "tag-1",
        "state": OrderState.OPEN,
        "filled": Decimal(0),
    }
    fields.update(overrides)
    return protocols.LegSnapshot(**fields)  # type: ignore[arg-type]


class TestLegSnapshot:
    def test_is_a_frozen_dataclass_with_contract_fields(self) -> None:
        assert dataclasses.is_dataclass(protocols.LegSnapshot)
        names = [f.name for f in dataclasses.fields(protocols.LegSnapshot)]
        assert names == [
            "client_order_id",
            "link_tag",
            "state",
            "filled",
            "venue_order_id",
            "group_id",
            "trigger_level",
            "limit_level",
        ]
        with pytest.raises(dataclasses.FrozenInstanceError):
            _snapshot().filled = Decimal(1)  # type: ignore[misc]

    def test_carries_the_values_it_was_given(self) -> None:
        snap = _snapshot(state=OrderState.PARTIALLY_FILLED, filled=Decimal("0.25"))
        assert snap.client_order_id == "cid-1"
        assert snap.link_tag == "tag-1"
        assert snap.state is OrderState.PARTIALLY_FILLED
        assert snap.filled == Decimal("0.25")

    def test_link_tag_may_be_none(self) -> None:
        assert _snapshot(link_tag=None).link_tag is None

    def test_structurally_satisfies_sf_leg_snapshot(self) -> None:
        assert isinstance(_snapshot(), _SfLegSnapshot)

    def test_is_exported_from_package_root(self) -> None:
        assert market_connector.LegSnapshot is protocols.LegSnapshot


class _Venue:
    async def get_leg(
        self,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> protocols.LegSnapshot | None:
        return None

    async def list_open_legs(self, target: str) -> list[protocols.LegSnapshot]:
        return []


class TestSupportsLegLookup:
    def test_is_runtime_checkable_and_matches_both_methods(self) -> None:
        assert isinstance(_Venue(), protocols.SupportsLegLookup)

    def test_object_missing_a_method_does_not_match(self) -> None:
        class _OnlyGet:
            async def get_leg(self, target: str) -> None:
                return None

        assert not isinstance(_OnlyGet(), protocols.SupportsLegLookup)

    def test_is_exported_from_package_root(self) -> None:
        assert market_connector.SupportsLegLookup is protocols.SupportsLegLookup
