"""SupportsBracket / SupportsOco protocols, BracketHandle / OcoHandle, linked contract (#239).

Assumed shape (the issue fixes the names, not the field names):

    BracketHandle(entry_client_id, take_profit_client_id, stop_loss_client_id, emulated)
    OcoHandle(leg_client_ids: tuple[...], emulated)

Both are frozen dataclasses exported from ``market_connector`` and holding ids only.
Contract helper (market_connector.testing.contract):

    check_linked_protocol_capabilities(gateway) -> list[str]   # empty == consistent
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from market_connector.capabilities import LinkedMode, OrderCapabilities


def _caps(linked: frozenset[LinkedMode]) -> OrderCapabilities:
    return OrderCapabilities(
        order_types=frozenset(),
        trigger_kinds=frozenset(),
        trigger_references=frozenset(),
        supports_trailing=False,
        linked=linked,
        native_linked=bool(linked),
    )


def _bracket(**overrides: Any) -> Any:
    from market_connector import BracketHandle

    base: dict[str, Any] = {
        "entry_client_id": "e1",
        "take_profit_client_id": "tp1",
        "stop_loss_client_id": "sl1",
        "emulated": False,
    }
    return BracketHandle(**{**base, **overrides})


def _oco(**overrides: Any) -> Any:
    from market_connector import OcoHandle

    base: dict[str, Any] = {"leg_client_ids": ("a", "b"), "emulated": True}
    return OcoHandle(**{**base, **overrides})


class TestHandles:
    def test_bracket_handle_carries_client_id_per_leg(self) -> None:
        h = _bracket()
        assert (h.entry_client_id, h.take_profit_client_id, h.stop_loss_client_id) == (
            "e1",
            "tp1",
            "sl1",
        )
        assert h.emulated is False

    def test_oco_handle_carries_client_id_per_leg(self) -> None:
        h = _oco(leg_client_ids=("a", "b", "c"))
        assert tuple(h.leg_client_ids) == ("a", "b", "c")
        assert h.emulated is True

    @pytest.mark.parametrize("factory", [_bracket, _oco])
    def test_handles_are_frozen_dataclasses(self, factory: Any) -> None:
        h = factory()
        assert dataclasses.is_dataclass(h)
        with pytest.raises(dataclasses.FrozenInstanceError):
            h.emulated = not h.emulated

    @pytest.mark.parametrize("factory", [_bracket, _oco])
    def test_handles_are_value_equal_and_hashable(self, factory: Any) -> None:
        assert factory() == factory()
        assert hash(factory()) == hash(factory())

    @pytest.mark.parametrize("factory", [_bracket, _oco])
    def test_handles_hold_ids_only(self, factory: Any) -> None:
        for f in dataclasses.fields(factory()):
            value = getattr(factory(), f.name)
            flat = value if isinstance(value, tuple) else (value,)
            assert all(isinstance(v, str | int | bool) for v in flat), f.name

    def test_handles_live_in_market_connector_and_export_top_level(self) -> None:
        import market_connector

        assert market_connector.BracketHandle.__module__.startswith("market_connector")
        assert market_connector.OcoHandle.__module__.startswith("market_connector")


class _Bracketer:
    async def place_bracket(self, target: Any, entry: Any, take_profit: Any, stop_loss: Any) -> Any:
        return _bracket()


class _Ocoer:
    async def place_oco(self, target: Any, orders: Any) -> Any:
        return _oco()


class TestProtocols:
    def test_supports_bracket_runtime_checkable_structural(self) -> None:
        from market_connector.protocols import SupportsBracket

        assert isinstance(_Bracketer(), SupportsBracket)
        assert not isinstance(object(), SupportsBracket)
        assert not isinstance(_Ocoer(), SupportsBracket)

    def test_supports_oco_runtime_checkable_structural(self) -> None:
        from market_connector.protocols import SupportsOco

        assert isinstance(_Ocoer(), SupportsOco)
        assert not isinstance(object(), SupportsOco)
        assert not isinstance(_Bracketer(), SupportsOco)

    def test_exported_from_package_top_level(self) -> None:
        import market_connector
        from market_connector import protocols

        assert market_connector.SupportsBracket is protocols.SupportsBracket
        assert market_connector.SupportsOco is protocols.SupportsOco

    def test_execution_gateway_unchanged(self) -> None:
        from market_connector.protocols import ExecutionGateway

        for name in ("place_bracket", "place_oco", "place_oca"):
            assert name not in dir(ExecutionGateway)


class _Both(_Bracketer, _Ocoer):
    def __init__(self, linked: frozenset[LinkedMode]) -> None:
        self._linked = linked

    @property
    def order_capabilities(self) -> OrderCapabilities:
        return _caps(self._linked)


class _BracketOnly(_Bracketer):
    def __init__(self, linked: frozenset[LinkedMode]) -> None:
        self._linked = linked

    @property
    def order_capabilities(self) -> OrderCapabilities:
        return _caps(self._linked)


class TestLinkedCapabilityContract:
    def test_consistent_gateway_has_no_failures(self) -> None:
        from market_connector.testing.contract import check_linked_protocol_capabilities

        gw = _Both(frozenset({LinkedMode.BRACKET, LinkedMode.OCO}))
        assert check_linked_protocol_capabilities(gw) == []

    def test_supports_bracket_without_bracket_capability_fails(self) -> None:
        from market_connector.testing.contract import check_linked_protocol_capabilities

        failures = check_linked_protocol_capabilities(_BracketOnly(frozenset()))
        assert failures
        assert any("BRACKET" in f for f in failures)

    def test_supports_oco_without_oco_capability_fails(self) -> None:
        from market_connector.testing.contract import check_linked_protocol_capabilities

        failures = check_linked_protocol_capabilities(_Both(frozenset({LinkedMode.BRACKET})))
        assert any("OCO" in f for f in failures)

    def test_non_implementer_is_not_required_to_advertise(self) -> None:
        from market_connector.testing.contract import check_linked_protocol_capabilities

        class _Plain:
            @property
            def order_capabilities(self) -> OrderCapabilities:
                return _caps(frozenset())

        assert check_linked_protocol_capabilities(_Plain()) == []
