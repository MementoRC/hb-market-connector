"""SupportsBracket / SupportsOco protocols, BracketHandle / OcoHandle, linked contract (#239).

Approved API (option B):

    SupportsBracket.place_bracket(target, spec: LinkedOrderSpec, *, options=None) -> BracketHandle
    SupportsOco.place_oco(target, spec: LinkedOrderSpec, *, options=None) -> OcoHandle

    BracketHandle: group_id, entry_client_id, take_profit_client_id | None,
                   stop_loss_client_id | None, emulated
    OcoHandle: group_id, leg_client_ids, emulated   # ids in spec.ordered_legs() order

Both handles are runtime_checkable typing.Protocol classes exported from ``market_connector``
whose members are read-only ``@property`` declarations (covariant, so a frozen dataclass with
``group_id: str`` matches ``str | int``). They are not concrete classes. ``cancel_linked`` was
deferred by #239 and is now declared on both Protocols by issue #251 (see
test_cancel_linked_251.py).
Contract helper (market_connector.testing.contract):

    check_linked_protocol_capabilities(gateway) -> list[str]   # empty == consistent
"""

from __future__ import annotations

import dataclasses
import inspect
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import (
    LegRole,
    LegSpec,
    LinkedOrderSpec,
    LinkKind,
    OrderType,
    TradeType,
)

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


def _leg(role: LegRole, side: TradeType, price: str, order_type: OrderType) -> LegSpec:
    return LegSpec(
        role=role, order_type=order_type, trade_type=side, amount=Decimal(1), price=Decimal(price)
    )


def _oco_spec() -> LinkedOrderSpec:
    return LinkedOrderSpec(
        kind=LinkKind.OCO,
        legs=(
            _leg(LegRole.OCO_LEG, TradeType.SELL, "110", OrderType.LIMIT),
            _leg(LegRole.OCO_LEG, TradeType.SELL, "120", OrderType.LIMIT),
        ),
    )


@dataclasses.dataclass(frozen=True)
class _BracketImpl:
    """Test-local stand-in for a downstream frozen handle (note ``group_id: str``)."""

    group_id: str = "g1"
    entry_client_id: str = "e1"
    take_profit_client_id: str | None = "tp1"
    stop_loss_client_id: str | None = "sl1"
    emulated: bool = False


@dataclasses.dataclass(frozen=True)
class _OcoImpl:
    group_id: str = "g2"
    leg_client_ids: tuple[str, ...] = ("a", "b")
    emulated: bool = True


def _bracket(**overrides: Any) -> _BracketImpl:
    return _BracketImpl(**overrides)


def _oco(**overrides: Any) -> _OcoImpl:
    return _OcoImpl(**overrides)


_BRACKET_MEMBERS = (
    "group_id",
    "entry_client_id",
    "take_profit_client_id",
    "stop_loss_client_id",
    "emulated",
)
_OCO_MEMBERS = ("group_id", "leg_client_ids", "emulated")


class TestHandleProtocols:
    @pytest.mark.parametrize("name", ["BracketHandle", "OcoHandle"])
    def test_handle_is_runtime_checkable_protocol(self, name: str) -> None:
        import market_connector

        proto = getattr(market_connector, name)
        assert getattr(proto, "_is_protocol", False) is True
        assert getattr(proto, "_is_runtime_protocol", False) is True
        assert proto.__module__.startswith("market_connector")

    @pytest.mark.parametrize(
        ("name", "members"), [("BracketHandle", _BRACKET_MEMBERS), ("OcoHandle", _OCO_MEMBERS)]
    )
    def test_members_are_read_only_properties(self, name: str, members: tuple[str, ...]) -> None:
        import market_connector

        proto = getattr(market_connector, name)
        for member in members:
            attr = proto.__dict__.get(member)
            assert isinstance(attr, property), member
            assert attr.fset is None, member
        declared = {n for n in vars(proto) if not n.startswith("_")}
        assert declared == set(members)

    @pytest.mark.parametrize("name", ["BracketHandle", "OcoHandle"])
    def test_handle_protocol_is_not_instantiable(self, name: str) -> None:
        import market_connector

        with pytest.raises(TypeError):
            getattr(market_connector, name)()

    def test_frozen_str_group_id_dataclass_satisfies_bracket_handle(self) -> None:
        from market_connector import BracketHandle

        assert isinstance(_bracket(), BracketHandle)
        assert not isinstance(object(), BracketHandle)
        assert not isinstance(_oco(), BracketHandle)

    def test_frozen_str_group_id_dataclass_satisfies_oco_handle(self) -> None:
        from market_connector import OcoHandle

        assert isinstance(_oco(), OcoHandle)
        assert not isinstance(object(), OcoHandle)
        assert not isinstance(_bracket(), OcoHandle)

    @pytest.mark.parametrize(
        ("tp", "sl"), [("tp1", None), (None, "sl1"), ("tp1", "sl1")], ids=["tp", "sl", "both"]
    )
    def test_bracket_with_optional_exits_satisfies_protocol(
        self, tp: str | None, sl: str | None
    ) -> None:
        from market_connector import BracketHandle

        h = _bracket(take_profit_client_id=tp, stop_loss_client_id=sl)
        assert isinstance(h, BracketHandle)
        assert (h.take_profit_client_id, h.stop_loss_client_id) == (tp, sl)

    def test_exported_from_top_level_and_in_all(self) -> None:
        import market_connector

        assert {"BracketHandle", "OcoHandle"} <= set(market_connector.__all__)


class _Bracketer:
    async def place_bracket(
        self, target: Any, spec: LinkedOrderSpec, *, options: Any = None
    ) -> Any:
        return _bracket()

    async def cancel_linked(self, handle: Any) -> Any:
        raise NotImplementedError


class _Ocoer:
    async def place_oco(self, target: Any, spec: LinkedOrderSpec, *, options: Any = None) -> Any:
        return _oco()

    async def cancel_linked(self, handle: Any) -> Any:
        raise NotImplementedError


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

    @pytest.mark.parametrize(
        ("proto", "method"), [("SupportsBracket", "place_bracket"), ("SupportsOco", "place_oco")]
    )
    def test_protocol_signature_is_target_spec_keyword_options(
        self, proto: str, method: str
    ) -> None:
        from market_connector import protocols

        sig = inspect.signature(getattr(getattr(protocols, proto), method))
        params = list(sig.parameters.values())
        assert [p.name for p in params] == ["self", "target", "spec", "options"]
        assert params[3].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[3].default is None

    @pytest.mark.parametrize(
        ("proto", "method"), [("SupportsBracket", "place_bracket"), ("SupportsOco", "place_oco")]
    )
    def test_protocol_method_is_coroutine(self, proto: str, method: str) -> None:
        from market_connector import protocols

        assert inspect.iscoroutinefunction(getattr(getattr(protocols, proto), method))

    @pytest.mark.parametrize("proto", ["SupportsBracket", "SupportsOco"])
    def test_cancel_linked_is_declared_by_251(self, proto: str) -> None:
        # #251 supersedes the #239 deferral: cancel_linked is now on both Protocols, and
        # still not on ExecutionGateway.
        from market_connector import protocols

        assert hasattr(getattr(protocols, proto), "cancel_linked")
        assert "cancel_linked" not in dir(protocols.ExecutionGateway)

    def test_exported_from_package_top_level(self) -> None:
        import market_connector
        from market_connector import protocols

        assert market_connector.SupportsBracket is protocols.SupportsBracket
        assert market_connector.SupportsOco is protocols.SupportsOco

    def test_execution_gateway_unchanged(self) -> None:
        from market_connector.protocols import ExecutionGateway

        for name in ("place_bracket", "place_oco", "place_oca"):
            assert name not in dir(ExecutionGateway)


class TestSpecOrdering:
    """The contract OcoHandle.leg_client_ids follows: LinkedOrderSpec.ordered_legs()."""

    def test_oco_leg_order_is_spec_order(self) -> None:
        spec = _oco_spec()
        assert [leg.price for leg in spec.ordered_legs()] == [Decimal(110), Decimal(120)]

    def test_oco_handle_leg_ids_align_with_ordered_legs(self) -> None:
        spec = _oco_spec()
        h = _oco(leg_client_ids=("x", "y"))
        assert len(h.leg_client_ids) == len(spec.ordered_legs())


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
