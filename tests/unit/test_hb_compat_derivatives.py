"""Acceptance tests for derivative hb_compat legacy API (issue #27).

The legacy classes mirror hummingbot's originals (same module paths, positional
constructors, and hummingbot's exact ``Position.__repr__`` format). Each one
converts to and from the canonical core primitives in ``market_connector.primitives``.
"""

import dataclasses
from decimal import Decimal
from enum import Enum

import pytest
from data_type_primitives import PositionSide
from market_connector.hb_compat.cancellation_result import CancellationResult
from market_connector.hb_compat.funding_info import FundingInfo, FundingInfoUpdate
from market_connector.hb_compat.position import Position

import market_connector.primitives as core


class LegacyPositionSide(Enum):
    """Stand-in for hummingbot's own PositionSide enum (foreign, value-compatible)."""

    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


def _legacy_funding_info() -> FundingInfo:
    return FundingInfo(
        "BTC-USDT",
        Decimal("50000.5"),
        Decimal("50010.25"),
        1700000000,
        Decimal("0.0001"),
    )


def _core_funding_info() -> core.FundingInfo:
    return core.FundingInfo(
        trading_pair="BTC-USDT",
        index_price=Decimal("50000.5"),
        mark_price=Decimal("50010.25"),
        next_funding_utc_timestamp=1700000000,
        rate=Decimal("0.0001"),
    )


def _legacy_position() -> Position:
    return Position(
        "ETH-USDT",
        PositionSide.LONG,
        Decimal("-12.5"),
        Decimal("3000.1"),
        Decimal("2.5"),
        Decimal("10"),
    )


def _core_position() -> core.Position:
    return core.Position(
        trading_pair="ETH-USDT",
        position_side=PositionSide.LONG,
        unrealized_pnl=Decimal("-12.5"),
        entry_price=Decimal("3000.1"),
        amount=Decimal("2.5"),
        leverage=Decimal("10"),
    )


class TestLegacyFundingInfo:
    def test_positional_init_and_properties(self) -> None:
        info = _legacy_funding_info()
        assert info.trading_pair == "BTC-USDT"
        assert info.index_price == Decimal("50000.5")
        assert info.mark_price == Decimal("50010.25")
        assert info.next_funding_utc_timestamp == 1700000000
        assert info.rate == Decimal("0.0001")

    def test_setters_update_fields(self) -> None:
        info = _legacy_funding_info()
        info.index_price = Decimal("1")
        info.mark_price = Decimal("2")
        info.next_funding_utc_timestamp = 3
        info.rate = Decimal("4")
        assert info.index_price == Decimal("1")
        assert info.mark_price == Decimal("2")
        assert info.next_funding_utc_timestamp == 3
        assert info.rate == Decimal("4")

    def test_trading_pair_is_read_only(self) -> None:
        info = _legacy_funding_info()
        with pytest.raises(AttributeError):
            info.trading_pair = "ETH-USDT"  # type: ignore[misc]

    def test_update_applies_non_none_fields_and_ignores_trading_pair(self) -> None:
        info = _legacy_funding_info()
        info.update(
            FundingInfoUpdate(
                trading_pair="ETH-USDT",
                index_price=Decimal("51000"),
                rate=Decimal("-0.0002"),
            )
        )
        assert info.trading_pair == "BTC-USDT"
        assert info.index_price == Decimal("51000")
        assert info.mark_price == Decimal("50010.25")
        assert info.next_funding_utc_timestamp == 1700000000
        assert info.rate == Decimal("-0.0002")

    def test_update_all_none_leaves_fields_unchanged(self) -> None:
        info = _legacy_funding_info()
        info.update(FundingInfoUpdate(trading_pair="BTC-USDT"))
        assert info.index_price == Decimal("50000.5")
        assert info.mark_price == Decimal("50010.25")
        assert info.next_funding_utc_timestamp == 1700000000
        assert info.rate == Decimal("0.0001")

    def test_funding_info_update_is_dataclass_with_defaults(self) -> None:
        assert dataclasses.is_dataclass(FundingInfoUpdate)
        update = FundingInfoUpdate(trading_pair="BTC-USDT")
        assert update.trading_pair == "BTC-USDT"
        assert update.index_price is None
        assert update.mark_price is None
        assert update.next_funding_utc_timestamp is None
        assert update.rate is None

    def test_to_primitive_returns_core_model(self) -> None:
        primitive = _legacy_funding_info().to_primitive()
        assert isinstance(primitive, core.FundingInfo)
        assert primitive == _core_funding_info()

    def test_from_primitive_builds_legacy(self) -> None:
        info = FundingInfo.from_primitive(_core_funding_info())
        assert isinstance(info, FundingInfo)
        assert info.trading_pair == "BTC-USDT"
        assert info.rate == Decimal("0.0001")

    def test_round_trip_from_core(self) -> None:
        core_info = _core_funding_info()
        assert FundingInfo.from_primitive(core_info).to_primitive() == core_info


class TestLegacyCancellationResult:
    def test_positional_init(self) -> None:
        result = CancellationResult("o-1", True)
        assert result.order_id == "o-1"
        assert result.success is True

    def test_tuple_unpacking(self) -> None:
        oid, ok = CancellationResult("o-2", False)
        assert oid == "o-2"
        assert ok is False

    def test_indexing(self) -> None:
        result = CancellationResult("o-3", True)
        assert result[0] == "o-3"
        assert result[1] is True

    def test_is_tuple(self) -> None:
        assert isinstance(CancellationResult("o-4", True), tuple)

    def test_to_primitive_returns_core_model(self) -> None:
        primitive = CancellationResult("o-1", True).to_primitive()
        assert isinstance(primitive, core.CancellationResult)
        assert primitive == core.CancellationResult(order_id="o-1", success=True)

    def test_from_primitive_builds_legacy(self) -> None:
        result = CancellationResult.from_primitive(
            core.CancellationResult(order_id="o-5", success=False)
        )
        assert isinstance(result, CancellationResult)
        assert result == CancellationResult("o-5", False)

    def test_round_trip_from_core(self) -> None:
        core_result = core.CancellationResult(order_id="o-6", success=True)
        assert CancellationResult.from_primitive(core_result).to_primitive() == core_result


class TestLegacyPosition:
    def test_positional_init_and_properties(self) -> None:
        position = _legacy_position()
        assert position.trading_pair == "ETH-USDT"
        assert position.position_side == PositionSide.LONG
        assert position.unrealized_pnl == Decimal("-12.5")
        assert position.entry_price == Decimal("3000.1")
        assert position.amount == Decimal("2.5")
        assert position.leverage == Decimal("10")

    def test_properties_are_read_only(self) -> None:
        position = _legacy_position()
        with pytest.raises(AttributeError):
            position.amount = Decimal("9")  # type: ignore[misc]

    def test_update_position_changes_only_given_fields(self) -> None:
        position = _legacy_position()
        position.update_position(unrealized_pnl=Decimal("7"), leverage=Decimal("20"))
        assert position.unrealized_pnl == Decimal("7")
        assert position.leverage == Decimal("20")
        assert position.trading_pair == "ETH-USDT"
        assert position.position_side == PositionSide.LONG
        assert position.entry_price == Decimal("3000.1")
        assert position.amount == Decimal("2.5")

    def test_update_position_all_fields(self) -> None:
        position = _legacy_position()
        position.update_position(
            position_side=PositionSide.SHORT,
            unrealized_pnl=Decimal("1"),
            entry_price=Decimal("2"),
            amount=Decimal("3"),
            leverage=Decimal("4"),
        )
        assert position.position_side == PositionSide.SHORT
        assert position.unrealized_pnl == Decimal("1")
        assert position.entry_price == Decimal("2")
        assert position.amount == Decimal("3")
        assert position.leverage == Decimal("4")

    def test_update_position_no_args_is_noop(self) -> None:
        position = _legacy_position()
        before = repr(position)
        position.update_position()
        assert repr(position) == before

    def test_repr_matches_hummingbot_format(self) -> None:
        position = _legacy_position()
        expected = (
            f"Position( trading_pair=ETH-USDT, position_side={PositionSide.LONG}, "
            f"unrealized_pnl={Decimal('-12.5')}, entry_price={Decimal('3000.1')}, "
            f"amount={Decimal('2.5')}, leverage={Decimal('10')})"
        )
        assert repr(position) == expected

    def test_to_primitive_returns_core_model(self) -> None:
        primitive = _legacy_position().to_primitive()
        assert isinstance(primitive, core.Position)
        assert primitive == _core_position()

    def test_to_primitive_maps_foreign_enum_by_value(self) -> None:
        position = Position(
            "ETH-USDT",
            LegacyPositionSide.SHORT,
            Decimal("0"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
        )
        primitive = position.to_primitive()
        assert primitive.position_side == PositionSide.SHORT
        assert isinstance(primitive.position_side, PositionSide)

    def test_from_primitive_builds_legacy(self) -> None:
        position = Position.from_primitive(_core_position())
        assert isinstance(position, Position)
        assert position.trading_pair == "ETH-USDT"
        assert position.leverage == Decimal("10")

    def test_round_trip_from_core(self) -> None:
        core_position = _core_position()
        assert Position.from_primitive(core_position).to_primitive() == core_position
