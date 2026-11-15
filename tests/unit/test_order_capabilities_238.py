"""OrderCapabilities / LinkedMode / HasCapabilities / capability contract check (#238).

Contract API under test (market_connector.testing.contract):

    async def check_order_capabilities(
        capabilities: OrderCapabilities,
        validate: Callable[[OrderType], Awaitable[None]],
        *,
        unsupported_probes: Iterable[OrderType] | None = None,
    ) -> list[str]

Returns human-readable failure strings (empty list == contract satisfied):
- each advertised type whose ``validate`` raises UnsupportedOrderError is a failure;
- each probed non-advertised type whose ``validate`` does NOT raise UnsupportedOrderError is a
  failure. ``unsupported_probes=None`` means every dtp ``OrderType`` not advertised.
``validate(order_type)`` is the gateway's placement validation (no network); any exception other
than UnsupportedOrderError is NOT a capability failure (e.g. a fake REST accepting the order).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from data_type_primitives import TriggerKind, TriggerReference
from data_type_primitives.common import OrderType

from market_connector.exceptions import UnsupportedOrderError
from market_connector.testing.contract import check_order_capabilities


def _caps(**overrides: Any) -> Any:
    from market_connector.capabilities import OrderCapabilities

    base: dict[str, Any] = {
        "order_types": frozenset({OrderType.LIMIT, OrderType.MARKET}),
        "trigger_kinds": frozenset(),
        "trigger_references": frozenset(),
        "supports_trailing": False,
        "linked": frozenset(),
        "native_linked": False,
    }
    return OrderCapabilities(**{**base, **overrides})


class TestLinkedMode:
    def test_members(self) -> None:
        from market_connector.capabilities import LinkedMode

        assert {m.name for m in LinkedMode} == {"BRACKET", "OCO"}


class TestOrderCapabilities:
    def test_fields_roundtrip(self) -> None:
        from market_connector.capabilities import LinkedMode

        caps = _caps(
            order_types=frozenset({OrderType.STOP_LOSS}),
            trigger_kinds=frozenset({TriggerKind.ABSOLUTE}),
            trigger_references=frozenset({TriggerReference.LAST}),
            supports_trailing=True,
            linked=frozenset({LinkedMode.OCO}),
            native_linked=True,
        )
        assert caps.order_types == frozenset({OrderType.STOP_LOSS})
        assert caps.trigger_kinds == frozenset({TriggerKind.ABSOLUTE})
        assert caps.trigger_references == frozenset({TriggerReference.LAST})
        assert caps.supports_trailing is True
        assert caps.linked == frozenset({LinkedMode.OCO})
        assert caps.native_linked is True

    def test_is_frozen_dataclass(self) -> None:
        caps = _caps()
        assert dataclasses.is_dataclass(caps)
        with pytest.raises(dataclasses.FrozenInstanceError):
            caps.supports_trailing = True

    def test_hashable_and_value_equal(self) -> None:
        assert hash(_caps()) == hash(_caps())
        assert _caps() == _caps()
        assert len({_caps(), _caps(), _caps(supports_trailing=True)}) == 2

    def test_importable_from_package_top_level(self) -> None:
        import market_connector
        from market_connector import capabilities

        assert market_connector.OrderCapabilities is capabilities.OrderCapabilities
        assert market_connector.LinkedMode is capabilities.LinkedMode
        assert market_connector.HasCapabilities is not None


class TestHasCapabilitiesProtocol:
    def test_runtime_checkable_structural(self) -> None:
        from market_connector.protocols import HasCapabilities

        class _With:
            @property
            def order_capabilities(self) -> Any:
                return _caps()

        assert isinstance(_With(), HasCapabilities)
        assert not isinstance(object(), HasCapabilities)

    def test_exported_from_package_top_level(self) -> None:
        import market_connector
        from market_connector import protocols

        assert market_connector.HasCapabilities is protocols.HasCapabilities

    def test_execution_gateway_unchanged(self) -> None:
        from market_connector.protocols import ExecutionGateway

        assert "order_capabilities" not in dir(ExecutionGateway)


def _validate_for(supported: set[OrderType]) -> Any:
    async def validate(order_type: OrderType) -> None:
        if order_type not in supported:
            raise UnsupportedOrderError(exchange="fake", order_type=order_type.name)

    return validate


class TestCheckOrderCapabilities:
    async def test_consistent_descriptor_has_no_failures(self) -> None:
        caps = _caps()
        failures = await check_order_capabilities(
            caps, _validate_for({OrderType.LIMIT, OrderType.MARKET})
        )
        assert failures == []

    async def test_negative_advertised_type_adapter_rejects(self) -> None:
        """Descriptor adds LIMIT_MAKER but the adapter raises UnsupportedOrderError for it."""
        caps = _caps(order_types=frozenset({OrderType.LIMIT, OrderType.LIMIT_MAKER}))
        failures = await check_order_capabilities(caps, _validate_for({OrderType.LIMIT}))
        assert failures
        assert any("LIMIT_MAKER" in f for f in failures)

    async def test_negative_unadvertised_type_adapter_accepts(self) -> None:
        """Adapter accepts a type the descriptor does not advertise: also a failure."""
        caps = _caps(order_types=frozenset({OrderType.LIMIT}))
        failures = await check_order_capabilities(
            caps,
            _validate_for({OrderType.LIMIT, OrderType.MARKET}),
            unsupported_probes=[OrderType.MARKET],
        )
        assert failures
        assert any("MARKET" in f for f in failures)

    async def test_non_unsupported_errors_are_not_capability_failures(self) -> None:
        async def validate(order_type: OrderType) -> None:
            if order_type == OrderType.LIMIT:
                raise RuntimeError("fake REST down")  # not a capability statement
            raise UnsupportedOrderError(exchange="fake", order_type=order_type.name)

        failures = await check_order_capabilities(
            _caps(order_types=frozenset({OrderType.LIMIT})), validate
        )
        assert failures == []

    async def test_default_probes_cover_every_unadvertised_dtp_type(self) -> None:
        probed: list[OrderType] = []

        async def validate(order_type: OrderType) -> None:
            probed.append(order_type)
            if order_type != OrderType.LIMIT:
                raise UnsupportedOrderError(exchange="fake", order_type=order_type.name)

        await check_order_capabilities(_caps(order_types=frozenset({OrderType.LIMIT})), validate)
        assert set(probed) == set(OrderType)

    def test_contract_base_class_is_exported(self) -> None:
        from market_connector.testing.contract import OrderCapabilitiesContractBase

        assert hasattr(OrderCapabilitiesContractBase, "validate_placement")
