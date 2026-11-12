"""Acceptance tests for GitHub issue #192.

Covers two dedupe requirements:
- ``hb_compat.funding_info`` re-exports the canonical ``data_type_primitives``
  ``FundingInfo``/``FundingInfoUpdate`` and provides module-level converters to
  and from the frozen ``market_connector.primitives.FundingInfo``.
- ``hb_compat.primitives`` maps the local StrEnum ``OrderType``/``TradeType``
  to and from the canonical ``data_type_primitives`` enums by member name.

Modules are imported whole (not symbol-by-symbol) so that a missing symbol
fails the individual test rather than breaking collection.
"""

from decimal import Decimal
from enum import Enum

import data_type_primitives
import pytest

from market_connector import primitives as core
from market_connector.hb_compat import funding_info as compat_funding
from market_connector.hb_compat import primitives as compat_primitives

_FUNDING_KWARGS = {
    "trading_pair": "BTC-USD",
    "index_price": Decimal("100"),
    "mark_price": Decimal("101"),
    "next_funding_utc_timestamp": 1700000000,
    "rate": Decimal("0.0001"),
}

_LOCAL_MEMBERS = [*compat_primitives.OrderType, *compat_primitives.TradeType]


def _canonical_funding_info():
    return data_type_primitives.FundingInfo(
        _FUNDING_KWARGS["trading_pair"],
        _FUNDING_KWARGS["index_price"],
        _FUNDING_KWARGS["mark_price"],
        _FUNDING_KWARGS["next_funding_utc_timestamp"],
        _FUNDING_KWARGS["rate"],
    )


def test_funding_info_is_canonical_reexport() -> None:
    assert compat_funding.FundingInfo is data_type_primitives.FundingInfo


def test_funding_info_update_is_canonical_reexport() -> None:
    assert compat_funding.FundingInfoUpdate is data_type_primitives.FundingInfoUpdate


def test_funding_info_to_primitive_returns_frozen_model_with_equal_fields() -> None:
    legacy = compat_funding.FundingInfo(**_FUNDING_KWARGS)

    primitive = compat_funding.funding_info_to_primitive(legacy)

    assert isinstance(primitive, core.FundingInfo)
    for field, expected in _FUNDING_KWARGS.items():
        assert getattr(primitive, field) == expected


def test_funding_info_from_primitive_returns_canonical_with_equal_fields() -> None:
    primitive = core.FundingInfo(**_FUNDING_KWARGS)

    canonical = compat_funding.funding_info_from_primitive(primitive)

    assert isinstance(canonical, data_type_primitives.FundingInfo)
    for field, expected in _FUNDING_KWARGS.items():
        assert getattr(canonical, field) == expected


def test_funding_info_round_trip_through_primitive_preserves_fields() -> None:
    original = _canonical_funding_info()

    primitive = compat_funding.funding_info_to_primitive(original)
    restored = compat_funding.funding_info_from_primitive(primitive)

    assert isinstance(restored, data_type_primitives.FundingInfo)
    for field, expected in _FUNDING_KWARGS.items():
        assert getattr(restored, field) == expected


@pytest.mark.parametrize("member", _LOCAL_MEMBERS, ids=lambda m: f"{type(m).__name__}.{m.name}")
def test_local_member_maps_to_canonical_by_name(member: Enum) -> None:
    canonical = compat_primitives.to_canonical(member)

    assert canonical.name == member.name
    assert type(canonical) is (
        data_type_primitives.OrderType
        if isinstance(member, compat_primitives.OrderType)
        else data_type_primitives.TradeType
    )


@pytest.mark.parametrize("member", _LOCAL_MEMBERS, ids=lambda m: f"{type(m).__name__}.{m.name}")
def test_local_member_round_trips_through_canonical(member: Enum) -> None:
    assert compat_primitives.from_canonical(compat_primitives.to_canonical(member)) is member


@pytest.mark.parametrize("member", _LOCAL_MEMBERS, ids=lambda m: f"{type(m).__name__}.{m.name}")
def test_local_member_remains_str_valued(member: Enum) -> None:
    assert isinstance(member, str)
    assert member.value == member.name


def test_from_canonical_rejects_amm_swap_without_local_equivalent() -> None:
    with pytest.raises(ValueError):
        compat_primitives.from_canonical(data_type_primitives.OrderType.AMM_SWAP)
